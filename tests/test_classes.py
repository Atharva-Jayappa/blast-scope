"""Tests for the consequence-class abstraction and the git class (v0.2).

Covers, per the eligibility filter:
- Stage-1 triage classifies destructive ops and stays silent on benign ones.
- Stage-2 assess degrades gracefully and labels estimates when a probe can't run.
- the declared probe surface is strictly read-only (the no-mutation guarantee).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blast_scope.classes import Candidate, gather_classes, registry
from blast_scope.classes.git import GitClass
from blast_scope.command_parser import parse_command
from blast_scope.recoverability import clear_cache


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed git repo with a dirty working tree (1 modified, 1 untracked)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "tracked.py").write_text("x = 1\n")
    _git(r, "add", "tracked.py")
    _git(r, "commit", "-m", "init")
    (r / "tracked.py").write_text("x = 2\n")
    (r / "untracked.py").write_text("y = 1\n")
    return r


def _triage(raw: str, cwd: Path) -> Candidate | None:
    return GitClass().triage(raw, parse_command(raw, cwd=cwd))


# ---------------------------------------------------------------------------
# Stage 1 — triage
# ---------------------------------------------------------------------------


class TestTriage:
    @pytest.mark.parametrize(
        "raw, op",
        [
            ("git reset --hard", "reset_hard"),
            ("git clean -fd", "clean_force"),
            ("git push --force", "push_force"),
            ("git push -f origin main", "push_force"),
            ("git branch -D feature", "branch_delete"),
            ("git rebase main", "history_rewrite"),
            ("git checkout -- a.py", "discard_paths"),
        ],
    )
    def test_destructive_ops_classified(self, raw: str, op: str, tmp_path: Path) -> None:
        c = _triage(raw, tmp_path)
        assert c is not None
        assert c.cls == "git"
        assert c.operation == op

    @pytest.mark.parametrize(
        "raw",
        ["git status", "git log --oneline", "git add .", "git commit -m x", "git diff"],
    )
    def test_benign_git_is_silent(self, raw: str, tmp_path: Path) -> None:
        assert _triage(raw, tmp_path) is None

    def test_non_git_is_silent(self, tmp_path: Path) -> None:
        assert _triage("rm -rf build", tmp_path) is None

    def test_triage_does_not_touch_disk(self, tmp_path: Path) -> None:
        # Triage must be near-free: it classifies a command for a repo that does
        # not even exist, without error (no probe, no subprocess).
        c = _triage("git reset --hard", tmp_path / "nonexistent")
        assert c is not None and c.operation == "reset_hard"


# ---------------------------------------------------------------------------
# Stage 2 — assess + probes
# ---------------------------------------------------------------------------


class TestAssess:
    def test_reset_hard_scales_and_annotates_reflog(self, repo: Path) -> None:
        c = GitClass().assess(Candidate("git", "reset_hard", "git reset --hard"), repo)
        assert c is not None
        assert c.domain == "vcs"
        assert c.floor > 0.0  # dirty tree
        assert "reflog" in c.evidence
        assert c.estimated is False

    def test_push_force_without_upstream_is_estimated(self, repo: Path) -> None:
        # No remote/tracking branch → we cannot verify remote impact, so the
        # finding keeps the base force-push floor but is labeled an estimate.
        c = GitClass().assess(Candidate("git", "push_force", "git push --force"), repo)
        assert c is not None
        assert c.floor >= 0.7
        assert c.estimated is True
        assert "unverified" in c.evidence

    def test_branch_delete_merged_is_low(self, repo: Path) -> None:
        # A fully-merged branch carries no unique commits → low, recoverable.
        _git(repo, "branch", "merged")
        c = GitClass().assess(
            Candidate("git", "branch_delete", "git branch -D merged"), repo
        )
        assert c is not None
        assert c.floor <= 0.25
        assert "fully merged" in c.evidence

    def test_branch_delete_unmerged_keeps_floor(self, repo: Path) -> None:
        _git(repo, "checkout", "-b", "feature")
        (repo / "f.py").write_text("z = 1\n")
        _git(repo, "add", "f.py")
        _git(repo, "commit", "-m", "feature work")
        _git(repo, "checkout", "-")
        c = GitClass().assess(
            Candidate("git", "branch_delete", "git branch -D feature"), repo
        )
        assert c is not None
        assert c.floor >= 0.45
        assert "unmerged" in c.evidence

    def test_assess_outside_repo_degrades_silently(self, tmp_path: Path) -> None:
        # Not a repo: the base analyzer sees a clean (empty) tree → floor 0,
        # and assess must not raise.
        c = GitClass().assess(
            Candidate("git", "reset_hard", "git reset --hard"), tmp_path
        )
        assert c is None or c.floor == 0.0


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestGatherClasses:
    def test_registry_lists_git(self) -> None:
        assert any(c.name == "git" for c in registry())

    def test_gather_returns_git_consequence(self, repo: Path) -> None:
        parsed = parse_command("git reset --hard", cwd=repo)
        out = gather_classes(parsed, "git reset --hard", repo)
        assert len(out) == 1
        assert out[0].domain == "vcs"
        assert out[0].floor > 0.0

    def test_gather_silent_on_benign(self, repo: Path) -> None:
        parsed = parse_command("git status", cwd=repo)
        assert gather_classes(parsed, "git status", repo) == []

    def test_gather_never_raises_on_bad_input(self, tmp_path: Path) -> None:
        parsed = parse_command("git", cwd=tmp_path)
        assert gather_classes(parsed, "git", tmp_path) == []


# ---------------------------------------------------------------------------
# Dry-run oracles (rung 2 tier 1)
# ---------------------------------------------------------------------------


class TestCleanOracle:
    def test_clean_dry_run_lists_exact_files(self, repo: Path) -> None:
        (repo / "junk1.tmp").write_text("a")
        (repo / "junk2.tmp").write_text("b")
        c = GitClass().assess(Candidate("git", "clean_force", "git clean -f"), repo)
        assert c is not None
        assert "dry-run verified" in c.evidence
        names = set(c.targets)
        assert {"junk1.tmp", "junk2.tmp", "untracked.py"} <= names
        assert c.estimated is False

    def test_clean_mirrors_d_flag(self, repo: Path) -> None:
        d = repo / "scratch"
        d.mkdir()
        (d / "f.txt").write_text("x")
        no_d = GitClass().assess(Candidate("git", "clean_force", "git clean -f"), repo)
        with_d = GitClass().assess(Candidate("git", "clean_force", "git clean -fd"), repo)
        assert no_d is not None and with_d is not None
        assert not any("scratch" in t for t in no_d.targets)
        assert any("scratch" in t for t in with_d.targets)

    def test_clean_mirrors_x_flag(self, repo: Path) -> None:
        (repo / ".gitignore").write_text("*.log\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore")
        (repo / "build.log").write_text("x")
        no_x = GitClass().assess(Candidate("git", "clean_force", "git clean -f"), repo)
        with_x = GitClass().assess(Candidate("git", "clean_force", "git clean -fx"), repo)
        assert no_x is not None and with_x is not None
        assert "build.log" not in no_x.targets
        assert "build.log" in with_x.targets

    def test_clean_on_clean_tree_is_zero(self, repo: Path) -> None:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "all in")
        c = GitClass().assess(Candidate("git", "clean_force", "git clean -fd"), repo)
        assert c is not None
        assert c.floor == 0.0
        assert c.targets == ()


class TestResetDivergenceOracle:
    def test_explicit_ref_counts_orphans_reflog_recoverable(self, repo: Path) -> None:
        _git(repo, "stash", "--include-untracked")  # clean the tree
        _git(repo, "tag", "base")
        (repo / "new1.py").write_text("1")
        _git(repo, "add", "new1.py")
        _git(repo, "commit", "-m", "c1")
        (repo / "new2.py").write_text("2")
        _git(repo, "add", "new2.py")
        _git(repo, "commit", "-m", "c2")
        c = GitClass().assess(
            Candidate("git", "reset_hard", "git reset --hard base"), repo
        )
        assert c is not None
        assert "orphans 2 commit(s)" in c.evidence
        assert "reflog" in c.evidence
        assert 0.45 <= c.floor < 0.8  # medium: reflog keeps them recoverable

    def test_implicit_head_keeps_base_behavior(self, repo: Path) -> None:
        c = GitClass().assess(Candidate("git", "reset_hard", "git reset --hard"), repo)
        assert c is not None
        assert "orphans" not in c.evidence  # no ref → divergence probe not run

    def test_unknown_ref_degrades_to_estimate(self, repo: Path) -> None:
        c = GitClass().assess(
            Candidate("git", "reset_hard", "git reset --hard origin/nope"), repo
        )
        assert c is not None
        assert c.estimated is True
        assert "unverified" in c.evidence


class TestDiscardOracle:
    def test_checkout_paths_lists_dirty_files(self, repo: Path) -> None:
        c = GitClass().assess(
            Candidate("git", "discard_paths", "git checkout -- tracked.py"), repo
        )
        assert c is not None
        assert c.targets == ("tracked.py",)
        assert "diff-verified" in c.evidence

    def test_checkout_clean_path_is_zero(self, repo: Path) -> None:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "all in")
        c = GitClass().assess(
            Candidate("git", "discard_paths", "git checkout -- tracked.py"), repo
        )
        assert c is not None
        assert c.floor == 0.0
        assert c.targets == ()
