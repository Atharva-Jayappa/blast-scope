"""Tests for blast_scope.hook — PreToolUse advisory + auto-snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_scope import hook, snapshot
from blast_scope.recoverability import clear_cache


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


def _payload(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


class TestRun:
    def test_non_bash_tool_is_silent(self, tmp_path: Path) -> None:
        out = hook.run({"tool_name": "Read", "tool_input": {}, "cwd": str(tmp_path)})
        assert out == {}

    def test_empty_command_is_silent(self, tmp_path: Path) -> None:
        assert hook.run(_payload("   ", tmp_path)) == {}

    def test_low_risk_is_silent(self, tmp_path: Path) -> None:
        # A read command is LOW — the hook stays out of the way entirely.
        (tmp_path / "notes.txt").write_text("hi")
        assert hook.run(_payload("cat notes.txt", tmp_path)) == {}

    def test_medium_risk_is_silent(self, tmp_path: Path) -> None:
        # An untracked plain file deletion lands at MEDIUM — below the advise
        # threshold, so the common case doesn't cost the agent any attention.
        (tmp_path / "scratch.txt").write_text("temp")
        out = hook.run(_payload("rm scratch.txt", tmp_path))
        assert out == {}
        assert snapshot.list_snapshots(tmp_path) == []

    def test_high_risk_advises_without_snapshot(self, tmp_path: Path) -> None:
        # precious_data (prod.db) floors at HIGH: we advise, but only CRITICAL
        # warrants spending disk on a snapshot.
        (tmp_path / "prod.db").write_text("rows")
        out = hook.run(_payload("rm prod.db", tmp_path))

        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "[blast-scope] HIGH" in ctx
        assert "Snapshot" not in ctx
        assert snapshot.list_snapshots(tmp_path) == []

    def test_critical_advises_and_snapshots(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("API_KEY=xyz")  # secret → CRITICAL
        out = hook.run(_payload("rm .env", tmp_path))

        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "[blast-scope] CRITICAL" in ctx
        assert "Snapshot" in ctx
        snaps = snapshot.list_snapshots(tmp_path)
        assert len(snaps) == 1
        assert snaps[0]["reason"] == "rm .env"

    def test_never_blocks(self, tmp_path: Path) -> None:
        # Even a critical command only advises — no deny decision is emitted.
        (tmp_path / ".env").write_text("API_KEY=xyz")
        out = hook.run(_payload("rm .env", tmp_path))
        hso = out["hookSpecificOutput"]
        assert hso.get("permissionDecision") in (None, "allow")

    def test_critical_skips_recoverable_targets_in_snapshot(self, tmp_path: Path) -> None:
        # The step is CRITICAL because of .env, but node_modules alongside it is
        # regenerable — Move 1 means we tar the secret and skip the rebuildable
        # tree (no redundant, unbounded archive).
        (tmp_path / ".env").write_text("API_KEY=xyz")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("x")

        out = hook.run(_payload("rm .env node_modules", tmp_path))
        assert "[blast-scope] CRITICAL" in out["hookSpecificOutput"]["additionalContext"]

        snaps = snapshot.list_snapshots(tmp_path)
        assert len(snaps) == 1
        archived = [Path(e["original"]).name for e in snaps[0]["entries"]]
        assert archived == [".env"]  # node_modules was not tarred

    def test_oversize_target_is_warned_not_snapshotted(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # When a destructive target exceeds the snapshot size cap, the hook warns
        # instead of silently taring a multi-GB tree.
        (tmp_path / ".env").write_text("API_KEY=xyz")
        monkeypatch.setattr(
            snapshot,
            "plan_snapshot",
            lambda targets, **kw: {
                "archive": [],
                "skipped_recoverable": [],
                "skipped_oversize": [str(tmp_path / ".env")],
            },
        )
        out = hook.run(_payload("rm .env", tmp_path))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "NOT snapshotted" in ctx
        assert snapshot.list_snapshots(tmp_path) == []

    def test_snapshot_enables_restore(self, tmp_path: Path) -> None:
        target = tmp_path / "secrets.json"
        target.write_text("{\"k\": \"v\"}")
        hook.run(_payload("rm secrets.json", tmp_path))

        target.unlink()  # the command "ran"
        snaps = snapshot.list_snapshots(tmp_path)
        assert snaps
        snapshot.restore_snapshot(snaps[0]["id"], root=tmp_path)
        assert target.read_text() == "{\"k\": \"v\"}"


class TestOracleSnapshot:
    """Oracle-discovered targets flow into the undo snapshot (rung 2)."""

    def test_git_clean_snapshots_oracle_targets(self, tmp_path: Path) -> None:
        import subprocess

        def g(*a: str) -> None:
            subprocess.run(["git", "-C", str(tmp_path), *a], capture_output=True)

        g("init")
        g("config", "user.email", "t@t.t")
        g("config", "user.name", "t")
        (tmp_path / "app.py").write_text("x = 1\n")
        g("add", "-A")
        g("commit", "-m", "init")
        # Untracked secret: `git clean -fdx` would delete it permanently. Its
        # path appears NOWHERE in the command — only the dry-run oracle can
        # discover it, and only the oracle-fed snapshot can save it.
        (tmp_path / ".env").write_text("KEY=irreplaceable\n")

        out = hook.run(_payload("git clean -fdx", tmp_path))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "CRITICAL" in ctx
        assert ".env" in ctx

        snaps = snapshot.list_snapshots(tmp_path)
        assert len(snaps) == 1
        archived = {Path(e["original"]).name for e in snaps[0]["entries"]}
        assert ".env" in archived

        # Simulate the deletion, then restore — the undo net works.
        (tmp_path / ".env").unlink()
        restored = snapshot.restore_snapshot(snaps[0]["id"], root=tmp_path)
        assert any(p.endswith(".env") for p in restored)
        assert (tmp_path / ".env").read_text() == "KEY=irreplaceable\n"
