"""Tests for the out-of-graph consequence analyzers (vcs / infra / config_refs)
and their integration as score floors in the risk scorer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from blast_scope import config_refs, infra, vcs
from blast_scope.command_parser import parse_command
from blast_scope.consequences import Consequence, gather
from blast_scope.recoverability import Recoverability, clear_cache
from blast_scope.risk_scorer import score_risk


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture()
def dirty_repo(tmp_path: Path) -> Path:
    """A git repo with one committed-then-modified file and one untracked file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("x = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")
    # Now make the tree dirty: modify tracked, add untracked.
    (repo / "tracked.py").write_text("x = 2\n")
    (repo / "untracked.py").write_text("y = 1\n")
    return repo


# ---------------------------------------------------------------------------
# vcs.analyze_git
# ---------------------------------------------------------------------------


class TestVcs:
    def test_non_git_returns_none(self, tmp_path: Path) -> None:
        parsed = parse_command("rm foo.py", cwd=tmp_path)
        assert vcs.analyze_git(parsed, "rm foo.py", tmp_path) is None

    def test_reset_hard_scales_with_modified(self, dirty_repo: Path) -> None:
        parsed = parse_command("git reset --hard", cwd=dirty_repo)
        c = vcs.analyze_git(parsed, "git reset --hard", dirty_repo)
        assert c is not None
        assert c.domain == "vcs"
        assert c.floor > 0.0
        assert "discard" in c.evidence

    def test_reset_hard_clean_tree_is_harmless(self, tmp_path: Path) -> None:
        repo = tmp_path / "clean"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "t")
        (repo / "a.py").write_text("a = 1\n")
        _git(repo, "add", "a.py")
        _git(repo, "commit", "-m", "init")
        parsed = parse_command("git reset --hard", cwd=repo)
        c = vcs.analyze_git(parsed, "git reset --hard", repo)
        assert c is not None
        assert c.floor == 0.0

    def test_clean_force_scales_with_untracked(self, dirty_repo: Path) -> None:
        parsed = parse_command("git clean -fd", cwd=dirty_repo)
        c = vcs.analyze_git(parsed, "git clean -fd", dirty_repo)
        assert c is not None
        assert c.floor > 0.0
        assert "untracked" in c.evidence

    def test_push_force_is_flagged(self, dirty_repo: Path) -> None:
        parsed = parse_command("git push --force", cwd=dirty_repo)
        c = vcs.analyze_git(parsed, "git push --force", dirty_repo)
        assert c is not None
        assert c.floor >= 0.7
        assert "force-push" in c.evidence

    def test_rebase_rewrites_history(self, dirty_repo: Path) -> None:
        parsed = parse_command("git rebase main", cwd=dirty_repo)
        c = vcs.analyze_git(parsed, "git rebase main", dirty_repo)
        assert c is not None
        assert "rewrites" in c.evidence

    def test_branch_force_delete(self, dirty_repo: Path) -> None:
        parsed = parse_command("git branch -D feature", cwd=dirty_repo)
        c = vcs.analyze_git(parsed, "git branch -D feature", dirty_repo)
        assert c is not None
        assert c.floor > 0.0

    def test_plain_status_is_none(self, dirty_repo: Path) -> None:
        parsed = parse_command("git status", cwd=dirty_repo)
        assert vcs.analyze_git(parsed, "git status", dirty_repo) is None


# ---------------------------------------------------------------------------
# infra.classify_infra
# ---------------------------------------------------------------------------


class TestInfra:
    def test_dockerfile(self) -> None:
        c = infra.classify_infra(Path("/proj/Dockerfile"))
        assert c is not None and c.domain == "infra" and c.floor == 0.6

    def test_terraform(self) -> None:
        c = infra.classify_infra(Path("/proj/main.tf"))
        assert c is not None and c.domain == "infra"

    def test_compose(self) -> None:
        assert infra.classify_infra(Path("/proj/docker-compose.yml")) is not None

    def test_github_workflow(self) -> None:
        c = infra.classify_infra(Path("/proj/.github/workflows/ci.yml"))
        assert c is not None

    def test_k8s_dir(self) -> None:
        c = infra.classify_infra(Path("/proj/k8s/deployment.yaml"))
        assert c is not None

    def test_plain_source_is_none(self) -> None:
        assert infra.classify_infra(Path("/proj/src/app.py")) is None


# ---------------------------------------------------------------------------
# config_refs.analyze_config_refs
# ---------------------------------------------------------------------------


class TestConfigRefs:
    def test_referenced_config_is_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "config.yaml").write_text("key: val\n")
        (tmp_path / "app.py").write_text('open("config.yaml")\n')
        (tmp_path / "loader.py").write_text('CONFIG = "config.yaml"\n')
        c = config_refs.analyze_config_refs(tmp_path / "config.yaml", tmp_path)
        assert c is not None
        assert c.domain == "config"
        assert c.floor >= 0.45

    def test_unreferenced_config_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "orphan.yaml").write_text("key: val\n")
        (tmp_path / "app.py").write_text("x = 1\n")
        assert config_refs.analyze_config_refs(tmp_path / "orphan.yaml", tmp_path) is None

    def test_non_config_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("x = 1\n")
        assert config_refs.analyze_config_refs(tmp_path / "main.py", tmp_path) is None

    def test_no_root_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "config.yaml").write_text("k: v\n")
        assert config_refs.analyze_config_refs(tmp_path / "config.yaml", None) is None

    def test_floor_scales_with_references(self, tmp_path: Path) -> None:
        (tmp_path / "settings.json").write_text("{}\n")
        for i in range(5):
            (tmp_path / f"m{i}.py").write_text('load("settings.json")\n')
        c = config_refs.analyze_config_refs(tmp_path / "settings.json", tmp_path)
        assert c is not None
        assert c.floor > 0.45


# ---------------------------------------------------------------------------
# consequences.gather
# ---------------------------------------------------------------------------


class TestGather:
    def test_gather_infra_target(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")
        parsed = parse_command("rm Dockerfile", cwd=tmp_path)
        out = gather(parsed, "rm Dockerfile", tmp_path, tmp_path)
        assert any(c.domain == "infra" for c in out)


# ---------------------------------------------------------------------------
# scorer integration: consequences raise the floor, before recoverability caps
# ---------------------------------------------------------------------------


def _rec(category: str, irr: float) -> Recoverability:
    return Recoverability(
        category=category, irrecoverability=irr, reversible=irr < 0.5, reason="test"
    )


class TestScorerFloor:
    def test_consequence_raises_score(self) -> None:
        parsed = parse_command("rm something.txt", cwd=Path.cwd())
        base = score_risk(parsed, [])
        raised = score_risk(parsed, [], None, [Consequence("infra", 0.6, "infra")])
        assert raised["score"] >= 0.6
        assert raised["score"] >= base["score"]
        assert "infra" in raised["evidence"]

    def test_regenerable_cap_beats_consequence_floor(self) -> None:
        # A regenerable target stays low even if a consequence wants to raise it:
        # the cap is applied AFTER the floor.
        parsed = parse_command("rm bundle.js", cwd=Path.cwd())
        rec = _rec("regenerable", 0.05)
        out = score_risk(parsed, [], rec, [Consequence("config", 0.6, "ref")])
        assert out["score"] <= 0.15

    def test_no_consequence_for_read_command(self) -> None:
        # weight == 0 (read) → consequences must not raise the score.
        parsed = parse_command("cat notes.txt", cwd=Path.cwd())
        out = score_risk(parsed, [], None, [Consequence("infra", 0.6, "x")])
        assert out["score"] < 0.6
