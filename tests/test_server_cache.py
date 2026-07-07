"""Regression: a long-lived process must not serve stale verdicts (field report #1).

The MCP server is a long-lived process, and agents repeat the same command as the
world changes. A cached working-tree state that outlived one assessment made
``git reset --hard`` keep its first verdict — LOW on a clean tree — even after the
tree went dirty (stale-LOW-after-danger, the direction that bites). Each
``assess()`` call must read fresh git state; the hook path was already safe
because it is process-per-command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blast_scope.recoverability import clear_cache
from blast_scope.server import assess


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    for i in range(4):
        (r / f"f{i}.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def test_reset_hard_verdict_tracks_worktree_in_one_process(repo: Path) -> None:
    # Clean tree → nothing to discard → low.
    assert assess("git reset --hard", cwd=str(repo))["severity"] == "low"

    # Dirty four files in the SAME process, no manual cache clear.
    for i in range(4):
        (repo / f"f{i}.py").write_text("x = 2\n")
    dirty = assess("git reset --hard", cwd=str(repo))
    assert dirty["severity"] != "low"  # must reflect the now-dirty tree, not stale LOW

    # Inverse: restore the committed content (tree clean again) → back to low,
    # proving the cache isn't merely stuck on the second reading either.
    for i in range(4):
        (repo / f"f{i}.py").write_text("x = 1\n")
    assert assess("git reset --hard", cwd=str(repo))["severity"] == "low"
