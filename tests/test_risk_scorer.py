"""Tests for blast_scope.risk_scorer."""

from __future__ import annotations

import pytest

from blast_scope.command_effects import classify_effect
from blast_scope.command_parser import ParsedCommand
from blast_scope.consequences import Consequence
from blast_scope.graph_resolver import GraphResolution, ResolvedNode
from blast_scope.recoverability import Recoverability
from blast_scope.risk_scorer import (
    RiskAssessment,
    score_risk,
    _score_to_severity,
    _severity_to_recommendation,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_parsed(
    command: str = "rm",
    targets: list[str] | None = None,
    flags: list[str] | None = None,
    intent: str = "destructive",
    recursive: bool = False,
    reversible: bool = False,
    weight: float | None = None,
) -> ParsedCommand:
    if weight is None:
        weight = classify_effect(command, flags or [], targets or []).weight
    return ParsedCommand(
        command=command,
        targets=targets or [],
        write_targets=targets or [],
        flags=flags or [],
        intent=intent,
        weight=weight,
        recursive=recursive,
        reversible=reversible,
    )


def _make_resolution(
    target_path: str = "/project/config.py",
    in_degree: int = 0,
    total_affected: int = 0,
    nodes_in_file: list[str] | None = None,
    affected_nodes: list[ResolvedNode] | None = None,
) -> GraphResolution:
    return GraphResolution(
        target_path=target_path,
        nodes_in_file=nodes_in_file or ["config.py", "config.py::load"],
        affected_nodes=affected_nodes or [],
        in_degree=in_degree,
        total_affected=total_affected,
    )


# ---------------------------------------------------------------------------
# Severity / recommendation mapping
# ---------------------------------------------------------------------------


class TestSeverityMapping:
    def test_critical(self) -> None:
        assert _score_to_severity(0.85) == "critical"
        assert _score_to_severity(1.0) == "critical"
        assert _score_to_severity(0.8) == "critical"

    def test_high(self) -> None:
        assert _score_to_severity(0.5) == "high"
        assert _score_to_severity(0.79) == "high"

    def test_medium(self) -> None:
        assert _score_to_severity(0.2) == "medium"
        assert _score_to_severity(0.49) == "medium"

    def test_low(self) -> None:
        assert _score_to_severity(0.0) == "low"
        assert _score_to_severity(0.19) == "low"


class TestRecommendationMapping:
    def test_block(self) -> None:
        assert _severity_to_recommendation("critical") == "block"

    def test_confirm(self) -> None:
        assert _severity_to_recommendation("high") == "confirm"
        assert _severity_to_recommendation("medium") == "confirm"

    def test_proceed(self) -> None:
        assert _severity_to_recommendation("low") == "proceed"


# ---------------------------------------------------------------------------
# Golden examples from the spec
# ---------------------------------------------------------------------------


class TestGoldenExamples:
    def test_rm_rf_logs_low_risk(self) -> None:
        """rm -rf ./logs — 0 importers, not tracked, outside src tree → LOW, proceed."""
        parsed = _make_parsed(
            command="rm",
            targets=["/project/logs"],
            flags=["-rf"],
            intent="destructive",
            recursive=True,
            reversible=False,
        )
        # Empty resolution — logs not in graph
        resolutions = [_make_resolution(
            target_path="/project/logs",
            in_degree=0,
            nodes_in_file=[],  # not in graph
            affected_nodes=[],
        )]
        result = score_risk(parsed, resolutions)
        assert result["severity"] == "low"
        assert result["recommendation"] == "proceed"

    def test_rm_rf_config_critical_risk(self) -> None:
        """rm -rf ./config — 8 importers, runtime-loaded, no backup → CRITICAL, block."""
        parsed = _make_parsed(
            command="rm",
            targets=["/project/config"],
            flags=["-rf"],
            intent="destructive",
            recursive=True,
            reversible=False,
        )
        resolutions = [_make_resolution(
            target_path="/project/config",
            in_degree=8,
            total_affected=10,
            nodes_in_file=["config.py", "config.py::load"],
            affected_nodes=[
                ResolvedNode(qualified_name=f"file{i}.py", kind="File", file_path=f"file{i}.py", depth=1)
                for i in range(10)
            ],
        )]
        result = score_risk(parsed, resolutions)
        assert result["severity"] == "critical"
        assert result["recommendation"] == "block"
        assert result["score"] >= 0.8


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------


class TestReadCommands:
    def test_cat_always_low(self) -> None:
        """cat has weight 0.0, so score should always be 0.0 = low."""
        parsed = _make_parsed(command="cat", intent="read")
        resolutions = [_make_resolution(in_degree=10)]
        result = score_risk(parsed, resolutions)
        assert result["score"] == 0.0
        assert result["severity"] == "low"
        assert result["recommendation"] == "proceed"

    def test_ls_always_low(self) -> None:
        parsed = _make_parsed(command="ls", intent="read")
        result = score_risk(parsed, [])
        assert result["severity"] == "low"
        assert result["recommendation"] == "proceed"


# ---------------------------------------------------------------------------
# Recursive flag impact
# ---------------------------------------------------------------------------


class TestRecursiveImpact:
    def test_recursive_increases_score(self) -> None:
        """Same command with -r should score higher than without."""
        base = _make_parsed(command="rm", recursive=False, reversible=False)
        recursive = _make_parsed(command="rm", recursive=True, reversible=False)
        res = [_make_resolution(in_degree=5)]

        base_result = score_risk(base, res)
        recursive_result = score_risk(recursive, res)

        assert recursive_result["score"] > base_result["score"]


# ---------------------------------------------------------------------------
# Reversibility impact
# ---------------------------------------------------------------------------


class TestReversibilityImpact:
    def test_reversible_halves_score(self) -> None:
        """Git-tracked targets should reduce the score by half."""
        not_rev = _make_parsed(command="rm", reversible=False, recursive=False)
        rev = _make_parsed(command="rm", reversible=True, recursive=False)
        res = [_make_resolution(in_degree=5)]

        not_rev_result = score_risk(not_rev, res)
        rev_result = score_risk(rev, res)

        # Reversible should be approximately half
        assert rev_result["score"] < not_rev_result["score"]
        assert abs(rev_result["score"] - not_rev_result["score"] / 2.0) < 0.01


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_score_clamped_at_zero(self) -> None:
        result = score_risk(_make_parsed(command="cat"), [])
        assert result["score"] >= 0.0

    def test_score_clamped_at_one(self) -> None:
        parsed = _make_parsed(command="dd", recursive=True, reversible=False)
        res = [_make_resolution(in_degree=20)]
        result = score_risk(parsed, res)
        assert result["score"] <= 1.0

    def test_no_resolutions(self) -> None:
        parsed = _make_parsed(command="rm")
        result = score_risk(parsed, [])
        # Should still produce a valid result
        assert result["score"] >= 0.0
        assert result["severity"] in ("low", "medium", "high", "critical")

    def test_zero_in_degree_with_graph_data(self) -> None:
        """File is in graph but has zero importers — should still get a baseline."""
        parsed = _make_parsed(command="rm")
        res = [_make_resolution(in_degree=0, nodes_in_file=["file.py"])]
        result = score_risk(parsed, res)
        assert result["score"] > 0.0  # baseline kicks in

    def test_unknown_command(self) -> None:
        parsed = _make_parsed(command="mycustomtool", intent="unknown")
        result = score_risk(parsed, [])
        # Uses default weight 0.3
        assert result["score"] > 0.0

    def test_rationale_contains_info(self) -> None:
        parsed = _make_parsed(command="rm", targets=["/project/config.py"])
        res = [_make_resolution(in_degree=3, total_affected=5)]
        result = score_risk(parsed, res)
        assert "rm" in result["rationale"]
        assert "config.py" in result["rationale"]
        assert "3 direct importer" in result["rationale"]

    def test_state_tied_consequence_does_not_leak_filesystem_reason(self) -> None:
        """git/docker/sql/packages must explain via the consequence, not a bogus path.

        Regression: the parser hands state-tied commands their subcommand token
        (`reset`) as a filesystem target, so recoverability classified it
        "absent" and the reason "path does not exist — nothing to lose" led the
        evidence and rationale of `git reset --hard`. The explanation must lead
        with the git consequence, and the bogus filesystem reason must not appear.
        """
        parsed = _make_parsed(command="git", targets=["/project/reset"], intent="destructive")
        rec = Recoverability(
            category="absent",
            irrecoverability=0.0,
            reversible=True,
            reason="path does not exist — nothing to lose",
        )
        cons = [Consequence("vcs", 0.45, "git reset --hard would discard 1 file(s)")]
        result = score_risk(parsed, [], recoverability=rec, consequences=cons)

        assert result["evidence"] == ["git reset --hard would discard 1 file(s)"]
        assert result["rationale"].startswith("git reset --hard would discard")
        assert not any("path does not exist" in e for e in result["evidence"])
        assert "path does not exist" not in result["rationale"]

    def test_affected_nodes_aggregated(self) -> None:
        nodes = [
            ResolvedNode(qualified_name=f"f{i}.py", kind="File", file_path=f"f{i}.py", depth=1)
            for i in range(3)
        ]
        res1 = _make_resolution(affected_nodes=nodes[:2])
        res2 = _make_resolution(affected_nodes=nodes[2:])
        result = score_risk(_make_parsed(), [res1, res2])
        assert len(result["affected_nodes"]) == 3
