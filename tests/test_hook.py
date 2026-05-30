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

    def test_advisory_context_is_returned(self, tmp_path: Path) -> None:
        out = hook.run(_payload("ls -la", tmp_path))
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "[blast-scope]" in out["hookSpecificOutput"]["additionalContext"]

    def test_never_blocks(self, tmp_path: Path) -> None:
        # Even a critical command only advises — no deny decision is emitted.
        secret = tmp_path / ".env"
        secret.write_text("API_KEY=xyz")
        out = hook.run(_payload("rm .env", tmp_path))
        hso = out["hookSpecificOutput"]
        assert hso.get("permissionDecision") in (None, "allow")

    def test_risky_destructive_command_snapshots(self, tmp_path: Path) -> None:
        target = tmp_path / "prod.db"  # precious_data → medium+ severity
        target.write_text("rows")
        out = hook.run(_payload("rm prod.db", tmp_path))

        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Snapshot" in ctx
        snaps = snapshot.list_snapshots(tmp_path)
        assert len(snaps) == 1
        assert snaps[0]["reason"] == "rm prod.db"

    def test_low_risk_command_does_not_snapshot(self, tmp_path: Path) -> None:
        # A read command should never trigger a snapshot.
        (tmp_path / "notes.txt").write_text("hi")
        hook.run(_payload("cat notes.txt", tmp_path))
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
