"""End-to-end tests for blast-scope.

Tests the full pipeline: parse command → resolve graph → score risk,
using the sample_project fixture with known ground-truth dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_scope.command_parser import parse_command
from blast_scope.graph_resolver import GraphResolver
from blast_scope.recoverability import classify_path
from blast_scope.risk_scorer import score_risk

SAMPLE_PROJECT = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture()
def resolver(tmp_path: Path) -> GraphResolver:
    """Build a GraphResolver with a populated graph over sample_project."""
    db_path = tmp_path / "e2e_graph.db"
    r = GraphResolver(SAMPLE_PROJECT, db_path=db_path)
    r.build_graph()
    return r


class TestFullPipeline:
    def test_rm_rf_config_scores_above_baseline(self, resolver: GraphResolver) -> None:
        """rm -rf ./config.py on a file imported by 2 other files → medium+.

        config.py is git-tracked (recoverable from history), so the two-axis
        model treats it as medium rather than critical — but its 2 importers
        keep it well above a file nothing depends on. With a real project
        (8+ importers) this would escalate to high/critical.
        """
        parsed = parse_command("rm -rf ./config.py", cwd=SAMPLE_PROJECT)
        resolutions = resolver.resolve_paths(
            [Path(t) for t in parsed["targets"]]
        )
        recoverability = classify_path(Path(parsed["targets"][0]))
        result = score_risk(parsed, resolutions, recoverability)

        assert result["severity"] in ("medium", "high", "critical")
        assert result["recommendation"] in ("confirm", "block")
        assert result["score"] >= 0.2

    def test_cat_main_is_low(self, resolver: GraphResolver) -> None:
        """cat main.py is a read command → always LOW."""
        parsed = parse_command("cat main.py", cwd=SAMPLE_PROJECT)
        resolutions = resolver.resolve_paths(
            [Path(t) for t in parsed["targets"]]
        )
        result = score_risk(parsed, resolutions)

        assert result["severity"] == "low"
        assert result["recommendation"] == "proceed"
        assert result["score"] == 0.0

    def test_rm_nonexistent_is_low(self, resolver: GraphResolver) -> None:
        """rm on a file not in the graph → low risk (baseline only)."""
        parsed = parse_command("rm ./logs/old.log", cwd=SAMPLE_PROJECT)
        resolutions = resolver.resolve_paths(
            [Path(t) for t in parsed["targets"]]
        )
        result = score_risk(parsed, resolutions)

        assert result["severity"] == "low"
        assert result["recommendation"] == "proceed"

    def test_rm_db_is_medium_or_higher(self, resolver: GraphResolver) -> None:
        """rm db.py — imported by main.py, so some risk."""
        parsed = parse_command("rm ./db.py", cwd=SAMPLE_PROJECT)
        resolutions = resolver.resolve_paths(
            [Path(t) for t in parsed["targets"]]
        )
        result = score_risk(parsed, resolutions)

        assert result["score"] > 0.0
        assert len(result["affected_nodes"]) >= 1

    def test_rm_main_has_no_importers(self, resolver: GraphResolver) -> None:
        """rm main.py — nothing imports main, so lower risk than config."""
        parsed_main = parse_command("rm ./main.py", cwd=SAMPLE_PROJECT)
        parsed_config = parse_command("rm ./config.py", cwd=SAMPLE_PROJECT)

        res_main = resolver.resolve_paths([Path(t) for t in parsed_main["targets"]])
        res_config = resolver.resolve_paths([Path(t) for t in parsed_config["targets"]])

        score_main = score_risk(parsed_main, res_main)
        score_config = score_risk(parsed_config, res_config)

        assert score_main["score"] <= score_config["score"]

    def test_no_project_root_still_works(self) -> None:
        """Without graph data, scoring still works using command weight alone."""
        parsed = parse_command("rm -rf ./config", cwd=SAMPLE_PROJECT)
        result = score_risk(parsed, [])

        assert result["severity"] in ("low", "medium", "high", "critical")
        assert result["score"] >= 0.0


class TestAutoIndex:
    """The server auto-indexes the project on first assess_command call."""

    def test_get_resolver_auto_builds_when_missing(self, tmp_path: Path) -> None:
        from blast_scope import server

        # Copy the sample project into tmp_path so we don't pollute the real fixture
        import shutil
        project = tmp_path / "proj"
        shutil.copytree(SAMPLE_PROJECT, project)

        # Reset module-level caches to isolate this test
        server._resolvers.clear()
        server._indexed_roots.clear()

        db_path = project / ".blast-scope" / "graph.db"
        assert not db_path.exists()

        resolver = server._get_resolver(project, auto_index=True)

        # After auto-indexing, the DB should exist
        assert db_path.exists()
        assert str(project.resolve()) in server._indexed_roots

    def test_assess_command_works_without_explicit_index(self, tmp_path: Path) -> None:
        """First call to assess_command on a fresh project triggers auto-index."""
        from blast_scope import server

        import shutil
        project = tmp_path / "proj"
        shutil.copytree(SAMPLE_PROJECT, project)

        server._resolvers.clear()
        server._indexed_roots.clear()

        result = server.assess_command(
            "rm ./config.py",
            cwd=str(project),
            project_root=str(project),
        )

        # The graph was auto-built — config.py has importers, so score > 0
        assert result["score"] > 0.0
        assert "chain" in result
        assert len(result["chain"]) == 1


class TestRationale:
    def test_rationale_mentions_target(self, resolver: GraphResolver) -> None:
        parsed = parse_command("rm ./config.py", cwd=SAMPLE_PROJECT)
        resolutions = resolver.resolve_paths(
            [Path(t) for t in parsed["targets"]]
        )
        result = score_risk(parsed, resolutions)

        assert "config.py" in result["rationale"]
        assert "rm" in result["rationale"]

    def test_rationale_mentions_importers(self, resolver: GraphResolver) -> None:
        parsed = parse_command("rm ./config.py", cwd=SAMPLE_PROJECT)
        resolutions = resolver.resolve_paths(
            [Path(t) for t in parsed["targets"]]
        )
        result = score_risk(parsed, resolutions)

        assert "importer" in result["rationale"]
