"""Regression tests for the security + correctness audit findings.

Each test pins a fix from the independent alien review so a future change can't
silently reopen the hole. Named by the finding id (S* security, C* correctness).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from blast_scope.command_parser import parse_command, real_command_verb
from blast_scope.resolution import _execute_readonly, resolve_segment
from blast_scope.server import assess


class TestS1SubstitutionExfil:
    def test_cat_secret_not_read_or_leaked(self, tmp_path: Path) -> None:
        (tmp_path / "SECRET.txt").write_text("SUPERSECRET-xyz")
        r = assess("rm -rf $(cat SECRET.txt)", cwd=str(tmp_path), env={})
        blob = r["rationale"] + " ".join(r.get("evidence", []))
        assert "SUPERSECRET" not in blob

    @pytest.mark.parametrize("inner", [
        "cat SECRET.txt", "head SECRET.txt", "tail -n1 SECRET.txt",
        "wc -c SECRET.txt", "readlink SECRET.txt", "realpath SECRET.txt",
    ])
    def test_content_readers_rejected(self, inner: str, tmp_path: Path) -> None:
        (tmp_path / "SECRET.txt").write_text("x")
        assert _execute_readonly(inner, tmp_path) is None

    @pytest.mark.parametrize("inner", ["ls /etc", "find / -name id_rsa", "cat /etc/shadow"])
    def test_outside_cwd_rejected(self, inner: str, tmp_path: Path) -> None:
        assert _execute_readonly(inner, tmp_path) is None

    def test_newline_metachar_rejected(self, tmp_path: Path) -> None:
        assert _execute_readonly("ls\ncat /etc/passwd", tmp_path) is None


class TestC1S3PrefixBlindness:
    @pytest.mark.parametrize("cmd", [
        "FOO=1 rm -rf /", "NODE_ENV=prod rm -rf ~/", "env rm -rf /",
        "timeout 60 rm -rf /", "nohup rm -rf /", "nice rm -rf /",
        "command rm -rf /", "busybox rm -rf /", "sudo env FOO=1 rm -rf /",
    ])
    def test_wrapped_root_delete_still_critical(self, cmd: str, tmp_path: Path) -> None:
        assert assess(cmd, cwd=str(tmp_path), env={})["severity"] == "critical"

    @pytest.mark.parametrize("raw, verb", [
        ("env NODE_ENV=prod rm -rf src", "rm"),
        ("timeout 60 git push", "git"),
        ("xargs rm", "rm"),
        ("/usr/bin/env rm x", "rm"),
        ("FOO=1 BAR=2 ls", "ls"),
        ("git status", "git"),
    ])
    def test_real_verb_peeling(self, raw: str, verb: str) -> None:
        assert real_command_verb(raw) == verb

    def test_unresolved_substitution_behind_sudo(self, tmp_path: Path) -> None:
        r = resolve_segment("sudo rm -rf $(nope)", tmp_path, {})
        assert any(h.kind == "unresolved_substitution" for h in r.hazards)


class TestS2SqlDoS:
    def test_recursive_cte_is_bounded(self, tmp_path: Path) -> None:
        from blast_scope.classes.sql import _assess_scoped_delete

        db = tmp_path / "app.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE t (id INTEGER)")
        con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
        con.commit()
        con.close()
        evil = ("DELETE FROM t WHERE id IN (WITH RECURSIVE c(x) AS "
                "(SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<20000000) SELECT x FROM c)")
        t0 = time.monotonic()
        result = _assess_scoped_delete(tmp_path, "app.db", "t", evil, False)
        assert time.monotonic() - t0 < 2.0  # bounded, not minutes
        assert result is None  # aborted probe degrades to silent


class TestC2RedirectMisparse:
    @pytest.mark.parametrize("cmd", ["make 2>&1", "pytest -q 2>&1", "cmd 1>&2"])
    def test_fd_dup_not_destructive(self, cmd: str) -> None:
        assert parse_command(cmd)["intent"] != "destructive"

    def test_real_clobber_still_destructive(self) -> None:
        assert parse_command("echo x > real.txt")["intent"] == "destructive"


class TestC3CheckoutPaths:
    @pytest.mark.parametrize("raw", [
        "git checkout ./src", "git checkout HEAD app.py",
        "git checkout main src/app.py", "git checkout src/", "git checkout -- app.py",
    ])
    def test_pathspec_forms_flagged(self, raw: str) -> None:
        from blast_scope import vcs
        sub, flags = vcs._subcommand(raw)
        assert vcs.destructive_op(sub, flags, raw) == "discard_paths"

    @pytest.mark.parametrize("raw", [
        "git checkout main", "git checkout release-1.2", "git checkout -b newbranch",
    ])
    def test_branch_switch_not_flagged(self, raw: str) -> None:
        from blast_scope import vcs
        sub, flags = vcs._subcommand(raw)
        assert vcs.destructive_op(sub, flags, raw) is None


class TestS4FindRootBound:
    def test_find_root_outside_cwd_not_probed(self, tmp_path: Path) -> None:
        from blast_scope.classes.find import FindClass
        from blast_scope.classes import Candidate

        c = FindClass().assess(
            Candidate("find", "delete", "find / -name '*.log' -delete"), tmp_path
        )
        assert c is None  # unbounded root ⇒ no probe, static classification stands


class TestC5ConfigRefs:
    def test_substring_not_counted_as_reference(self, tmp_path: Path) -> None:
        from blast_scope import config_refs

        (tmp_path / "config").write_text("k=v\n")
        # Substrings of "config" — none is the standalone word, so none counts.
        (tmp_path / "a.py").write_text("import configparser\n")
        (tmp_path / "b.py").write_text("x = 'configuration_string'\nreconfigure()\n")
        c = config_refs.analyze_config_refs(tmp_path / "config", tmp_path)
        assert c is None  # zero genuine word references

    def test_real_word_reference_still_counted(self, tmp_path: Path) -> None:
        from blast_scope import config_refs

        (tmp_path / "config").write_text("k=v\n")
        (tmp_path / "a.py").write_text("load('config')  # opens the config file\n")
        c = config_refs.analyze_config_refs(tmp_path / "config", tmp_path)
        assert c is not None and c.floor >= 0.4  # genuine reference still fires
