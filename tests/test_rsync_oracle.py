"""Tests for the rsync --delete dry-run oracle (classes/rsync.py)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from blast_scope.classes import Candidate
from blast_scope.classes.rsync import RsyncClass
from blast_scope.command_parser import parse_command

needs_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync not on PATH"
)


class TestTriage:
    def test_delete_flag_triaged(self, tmp_path: Path) -> None:
        raw = "rsync -a --delete src/ dst/"
        c = RsyncClass().triage(raw, parse_command(raw, cwd=tmp_path))
        assert c is not None and c.operation == "delete_sync"

    def test_delete_variants_triaged(self, tmp_path: Path) -> None:
        raw = "rsync -a --delete-after src/ dst/"
        c = RsyncClass().triage(raw, parse_command(raw, cwd=tmp_path))
        assert c is not None

    def test_plain_rsync_silent(self, tmp_path: Path) -> None:
        raw = "rsync -a src/ dst/"
        assert RsyncClass().triage(raw, parse_command(raw, cwd=tmp_path)) is None


class TestAssess:
    def test_remote_endpoint_is_estimate(self, tmp_path: Path) -> None:
        c = RsyncClass().assess(
            Candidate("rsync", "delete_sync", "rsync -a --delete src/ host:/backup/"),
            tmp_path,
        )
        assert c is not None and c.estimated is True
        assert "remote" in c.evidence

    def test_missing_rsync_degrades(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("blast_scope.classes.rsync._run_rsync", lambda a, c: None)
        c = RsyncClass().assess(
            Candidate("rsync", "delete_sync", "rsync -a --delete src/ dst/"), tmp_path
        )
        assert c is not None and c.estimated is True
        assert c.floor >= 0.35

    @needs_rsync
    def test_dry_run_lists_deletions(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "keep.txt").write_text("k")
        (dst / "keep.txt").write_text("k")
        (dst / "gone1.txt").write_text("x")
        (dst / "gone2.txt").write_text("x")
        c = RsyncClass().assess(
            Candidate("rsync", "delete_sync", "rsync -a --delete src/ dst/"), tmp_path
        )
        assert c is not None and c.estimated is False
        names = {Path(t).name for t in c.targets}
        assert names == {"gone1.txt", "gone2.txt"}
        assert "dry-run verified" in c.evidence

    @needs_rsync
    def test_dry_run_nothing_to_delete(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "dst").mkdir()
        c = RsyncClass().assess(
            Candidate("rsync", "delete_sync", "rsync -a --delete src/ dst/"), tmp_path
        )
        assert c is not None
        assert c.floor <= 0.1
        assert c.targets == ()
