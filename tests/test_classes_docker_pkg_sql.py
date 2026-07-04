"""Tests for the docker, packages, and sql consequence classes (v0.2).

Each class is checked for: correct triage of its destructive ops, silence on
benign ones, graceful degrade-to-estimate when a probe is unavailable, and a
read-only probe surface. Docker probes are monkeypatched so the suite never
depends on a running daemon; SQLite uses a real read-only probe (stdlib).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blast_scope.classes import Candidate
from blast_scope.classes.docker import DockerClass
from blast_scope.classes.packages import PackagesClass
from blast_scope.classes.sql import SqlClass
from blast_scope.command_parser import parse_command


def _cand(cls, raw: str, cwd: Path) -> Candidate | None:
    return cls.triage(raw, parse_command(raw, cwd=cwd))


# ===========================================================================
# Docker
# ===========================================================================


class TestDockerTriage:
    @pytest.mark.parametrize(
        "raw, op",
        [
            ("docker volume rm appdata", "volume_rm"),
            ("docker volume prune", "volume_prune"),
            ("docker system prune -a --volumes", "system_prune"),
            ("docker rm -f web", "container_rm"),
            ("docker container rm -f web db", "container_rm"),
            ("sudo docker volume rm appdata", "volume_rm"),
        ],
    )
    def test_destructive_ops(self, raw: str, op: str, tmp_path: Path) -> None:
        c = _cand(DockerClass(), raw, tmp_path)
        assert c is not None and c.operation == op

    @pytest.mark.parametrize(
        "raw",
        ["docker ps -a", "docker volume ls", "docker images", "docker rm web", "ls -la"],
    )
    def test_benign_is_silent(self, raw: str, tmp_path: Path) -> None:
        # note: `docker rm web` without -f is not flagged (matches scope: rm -f)
        assert _cand(DockerClass(), raw, tmp_path) is None


class TestDockerAssess:
    def test_volume_rm_degrades_to_estimate(self, tmp_path: Path, monkeypatch) -> None:
        # No daemon → estimate, labeled, but still a serious floor (data risk).
        monkeypatch.setattr("blast_scope.classes.docker._daemon_up", lambda cwd: False)
        c = DockerClass().assess(Candidate("docker", "volume_rm", "docker volume rm d", ("d",)), tmp_path)
        assert c is not None and c.estimated is True
        assert c.floor >= 0.7

    def test_volume_rm_existing_is_critical(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("blast_scope.classes.docker._daemon_up", lambda cwd: True)
        # inspect succeeds (exists), no container uses it.
        def fake_run(cwd, *args):
            if args[:2] == ("volume", "inspect"):
                return (True, '[{"Name":"d"}]')
            return (True, "")
        monkeypatch.setattr("blast_scope.classes.docker._run_docker", fake_run)
        c = DockerClass().assess(Candidate("docker", "volume_rm", "docker volume rm d", ("d",)), tmp_path)
        assert c is not None and c.estimated is False
        assert c.floor >= 0.85

    def test_volume_rm_missing_is_low(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("blast_scope.classes.docker._daemon_up", lambda cwd: True)
        monkeypatch.setattr("blast_scope.classes.docker._run_docker", lambda cwd, *a: (True, ""))
        c = DockerClass().assess(Candidate("docker", "volume_rm", "docker volume rm gone", ("gone",)), tmp_path)
        assert c is not None and c.floor <= 0.15

    def test_system_prune_volumes_estimate_is_high(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("blast_scope.classes.docker._daemon_up", lambda cwd: False)
        c = DockerClass().assess(
            Candidate("docker", "system_prune", "docker system prune -a --volumes"), tmp_path
        )
        assert c is not None and c.estimated is True and c.floor >= 0.7

    def test_container_rm_is_reversible_medium(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("blast_scope.classes.docker._daemon_up", lambda cwd: True)
        monkeypatch.setattr("blast_scope.classes.docker._run_docker", lambda cwd, *a: (True, "web"))
        c = DockerClass().assess(Candidate("docker", "container_rm", "docker rm -f web", ("web",)), tmp_path)
        assert c is not None
        assert 0.2 <= c.floor <= 0.5  # recreatable from image


# ===========================================================================
# Packages
# ===========================================================================


class TestPackages:
    @pytest.mark.parametrize(
        "raw, op",
        [
            ("pip uninstall flask", "pip_uninstall"),
            ("pip3 uninstall -y requests", "pip_uninstall"),
            ("uv pip uninstall numpy", "uv_uninstall"),
        ],
    )
    def test_triage(self, raw: str, op: str, tmp_path: Path) -> None:
        c = _cand(PackagesClass(), raw, tmp_path)
        assert c is not None and c.operation == op

    @pytest.mark.parametrize("raw", ["pip install flask", "pip list", "uv sync", "uv pip list"])
    def test_benign_is_silent(self, raw: str, tmp_path: Path) -> None:
        assert _cand(PackagesClass(), raw, tmp_path) is None

    def test_lockfile_present_is_low(self, tmp_path: Path) -> None:
        (tmp_path / "uv.lock").write_text("# lock\n")
        c = PackagesClass().assess(Candidate("packages", "pip_uninstall", "pip uninstall flask", ("flask",)), tmp_path)
        assert c is not None and c.floor <= 0.15
        assert c.estimated is False
        assert "uv.lock" in c.evidence

    def test_no_lockfile_is_medium(self, tmp_path: Path) -> None:
        c = PackagesClass().assess(Candidate("packages", "pip_uninstall", "pip uninstall flask", ("flask",)), tmp_path)
        assert c is not None and 0.2 <= c.floor < 0.5


# ===========================================================================
# SQL
# ===========================================================================


def _make_sqlite(path: Path, rows: int) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER)")
    con.executemany("INSERT INTO users VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


class TestSqlTriage:
    @pytest.mark.parametrize(
        "raw, op",
        [
            ('psql -c "DROP TABLE users"', "drop"),
            ('mysql -e "TRUNCATE events"', "truncate"),
            ('psql -c "DELETE FROM logs"', "delete_all"),
            ('sqlite3 app.db "DROP TABLE users"', "drop"),
        ],
    )
    def test_destructive_sql(self, raw: str, op: str, tmp_path: Path) -> None:
        c = _cand(SqlClass(), raw, tmp_path)
        assert c is not None and c.operation == op

    @pytest.mark.parametrize(
        "raw",
        [
            'psql -c "DELETE FROM logs WHERE id = 1"',  # has WHERE → benign
            'psql -c "SELECT * FROM users"',
            'mysql -e "INSERT INTO t VALUES (1)"',
            "ls -la",
        ],
    )
    def test_benign_is_silent(self, raw: str, tmp_path: Path) -> None:
        assert _cand(SqlClass(), raw, tmp_path) is None


class TestSqlAssess:
    def test_sqlite_probe_counts_rows(self, tmp_path: Path) -> None:
        db = tmp_path / "app.db"
        _make_sqlite(db, rows=42)
        c = SqlClass().assess(
            Candidate("sql", "drop", 'sqlite3 app.db "DROP TABLE users"', ("sqlite", "app.db")),
            tmp_path,
        )
        assert c is not None and c.estimated is False
        assert "42" in c.evidence
        assert c.floor >= 0.85  # critical: schema + rows, irreversible

    def test_sqlite_missing_table_is_low(self, tmp_path: Path) -> None:
        db = tmp_path / "app.db"
        _make_sqlite(db, rows=1)
        c = SqlClass().assess(
            Candidate("sql", "drop", 'sqlite3 app.db "DROP TABLE ghosts"', ("sqlite", "app.db")),
            tmp_path,
        )
        assert c is not None and c.floor <= 0.2

    def test_postgres_degrades_to_estimate(self, tmp_path: Path) -> None:
        # No driver / possibly-remote server → no probe, labeled estimate.
        c = SqlClass().assess(
            Candidate("sql", "drop", 'psql -c "DROP TABLE users"', ("postgres", "")), tmp_path
        )
        assert c is not None and c.estimated is True and c.floor >= 0.85
        assert "estimated" in c.evidence

    def test_transaction_lowers_floor(self, tmp_path: Path) -> None:
        c = SqlClass().assess(
            Candidate("sql", "drop", 'psql -c "BEGIN; DROP TABLE users"', ("postgres", "")),
            tmp_path,
        )
        assert c is not None and c.floor <= 0.6
        assert "ROLLBACK" in c.evidence

    def test_delete_no_where_estimate_is_high(self, tmp_path: Path) -> None:
        c = SqlClass().assess(
            Candidate("sql", "delete_all", 'mysql -e "DELETE FROM events"', ("mysql", "")),
            tmp_path,
        )
        assert c is not None and 0.5 <= c.floor < 0.85
