"""Tests for the find dry-run oracle (classes/find.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blast_scope.classes import Candidate
from blast_scope.classes.find import FindClass, _rewrite
from blast_scope.command_parser import parse_command


def _real_find_available() -> bool:
    """True when `find` on PATH is a real POSIX find (not Windows find.exe)."""
    try:
        proc = subprocess.run(
            ["find", ".", "-maxdepth", "0", "-print"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "."


needs_find = pytest.mark.skipif(
    not _real_find_available(), reason="no POSIX find on PATH"
)


class TestRewrite:
    def test_delete_becomes_print_with_depth(self) -> None:
        argv, subtree = _rewrite("find . -name '*.log' -delete")
        assert argv == ["find", ".", "-name", "*.log", "-print", "-depth"] or argv == [
            "find", ".", "-depth", "-name", "*.log", "-print",
        ]
        assert subtree is False

    def test_depth_inserted_after_paths(self) -> None:
        argv, _ = _rewrite("find src tests -name '*.tmp' -delete")
        assert argv is not None
        assert argv[:3] == ["find", "src", "tests"]
        assert "-depth" in argv and "-print" in argv and "-delete" not in argv

    def test_existing_depth_not_duplicated(self) -> None:
        argv, _ = _rewrite("find . -depth -name x -delete")
        assert argv is not None
        assert argv.count("-depth") == 1

    def test_exec_rm_clause_replaced_in_place(self) -> None:
        argv, subtree = _rewrite("find . -name '*.pyc' -exec rm {} ;")
        assert argv == ["find", ".", "-name", "*.pyc", "-print"]
        assert subtree is False

    def test_exec_rm_rf_marks_subtree_roots(self) -> None:
        argv, subtree = _rewrite("find . -type d -name build -exec rm -rf {} +")
        assert argv == ["find", ".", "-type", "d", "-name", "build", "-print"]
        assert subtree is True

    def test_exec_truncate_supported(self) -> None:
        argv, subtree = _rewrite("find . -name '*.py' -exec truncate -s 0 {} +")
        assert argv == ["find", ".", "-name", "*.py", "-print"]
        assert subtree is False

    def test_alternation_punts(self) -> None:
        argv, _ = _rewrite("find . -name a -delete -o -name b -print")
        assert argv is None

    def test_multiple_terminals_punt(self) -> None:
        argv, _ = _rewrite("find . -name a -delete -name b -delete")
        assert argv is None

    def test_benign_exec_punts(self) -> None:
        argv, _ = _rewrite("find . -name '*.py' -exec wc -l {} ;")
        assert argv is None


class TestTriage:
    def test_delete_triaged(self, tmp_path: Path) -> None:
        raw = "find . -name '*.tmp' -delete"
        c = FindClass().triage(raw, parse_command(raw, cwd=tmp_path))
        assert c is not None and c.operation == "delete"

    def test_destructive_exec_triaged(self, tmp_path: Path) -> None:
        raw = "find . -name '*.py' -exec truncate -s 0 {} +"
        c = FindClass().triage(raw, parse_command(raw, cwd=tmp_path))
        assert c is not None and c.operation == "exec"

    def test_readonly_find_silent(self, tmp_path: Path) -> None:
        raw = "find . -name '*.py' -print"
        assert FindClass().triage(raw, parse_command(raw, cwd=tmp_path)) is None

    def test_benign_exec_silent(self, tmp_path: Path) -> None:
        raw = "find . -exec wc -l {} ;"
        assert FindClass().triage(raw, parse_command(raw, cwd=tmp_path)) is None


@needs_find
class TestProbe:
    def test_delete_match_set_discovered(self, tmp_path: Path) -> None:
        (tmp_path / "a.log").write_text("x")
        (tmp_path / "b.log").write_text("x")
        (tmp_path / "keep.txt").write_text("x")
        c = FindClass().assess(
            Candidate("find", "delete", "find . -name '*.log' -delete"), tmp_path
        )
        assert c is not None
        assert len(c.targets) == 2
        assert all(t.endswith(".log") for t in c.targets)
        assert "dry-run" in c.evidence

    def test_prune_exclusion_respected(self, tmp_path: Path) -> None:
        # The -depth fix: with -prune in play, a naive rewrite (no -depth)
        # would still exclude vendor/ here, but GNU find's real -delete also
        # excludes it — what matters is both agree. Verify the preview does
        # NOT include the pruned directory's files.
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "v.log").write_text("x")
        (tmp_path / "app.log").write_text("x")
        c = FindClass().assess(
            Candidate(
                "find",
                "delete",
                "find . -path ./vendor -prune -o -name '*.log' -delete",
            ),
            tmp_path,
        )
        # This expression contains -o → the rewrite must punt, not lie.
        assert c is None

    def test_no_matches_notes_empty(self, tmp_path: Path) -> None:
        c = FindClass().assess(
            Candidate("find", "delete", "find . -name '*.nope' -delete"), tmp_path
        )
        assert c is not None
        assert c.targets == ()
        assert "matches nothing" in c.evidence
