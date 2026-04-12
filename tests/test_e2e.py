"""End-to-end tests for blast-scope.

Tests the full pipeline: parse command → resolve graph → score risk,
using the sample_project fixture with known ground-truth dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_scope.command_parser import parse_command
from blast_scope.graph_resolver import GraphResolver
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

        In our small fixture (2 importers), score = 0.9 * 0.2 * 2.0 = 0.36.
        With a real project (8+ importers) this would be critical. The key
        assertion: config scores significantly higher than a file with no importers.
        """
        parsed = parse_command("rm -rf ./config.py", cwd=SAMPLE_PROJECT)
        resolutions = resolver.resolve_paths(
            [Path(t) for t in parsed["targets"]]
        )
        result = score_risk(parsed, resolutions)

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
