"""Combine parser output and graph resolution into a risk score.

Pure functions — no side effects. Takes structured data from the command
parser and graph resolver and produces a human-readable risk assessment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from blast_scope.command_parser import ParsedCommand
from blast_scope.graph_resolver import GraphResolution, ResolvedNode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


class RiskAssessment(TypedDict):
    """Structured risk assessment for a shell command.

    Example::

        {
            "score": 0.9,
            "severity": "critical",
            "rationale": "rm targets config.py (8 importers, not git-tracked). CRITICAL.",
            "affected_nodes": [...],
            "recommendation": "block",
        }
    """

    score: float
    severity: str  # "low" | "medium" | "high" | "critical"
    rationale: str
    affected_nodes: list[ResolvedNode]
    recommendation: str  # "proceed" | "confirm" | "block"


# ---------------------------------------------------------------------------
# Command weight table
# ---------------------------------------------------------------------------

COMMAND_WEIGHTS: dict[str, float] = {
    # Destructive
    "rm": 0.9,
    "rmdir": 0.7,
    "truncate": 0.8,
    "dd": 1.0,
    "mkfs": 1.0,
    "shred": 1.0,
    # Modifying
    "mv": 0.6,
    "chmod": 0.5,
    "chown": 0.5,
    "sed": 0.4,
    # Additive
    "touch": 0.1,
    "mkdir": 0.1,
    "cp": 0.2,
    # Read
    "cat": 0.0,
    "head": 0.0,
    "tail": 0.0,
    "less": 0.0,
    "more": 0.0,
    "grep": 0.0,
    "find": 0.0,
    "ls": 0.0,
    "wc": 0.0,
    "diff": 0.0,
    "file": 0.0,
    "stat": 0.0,
}

DEFAULT_WEIGHT: float = 0.3

# In-degree normalization ceiling (10+ importers = max risk)
_IN_DEGREE_CEILING: int = 10

# Baseline in-degree when no graph data is available
_BASELINE_IN_DEGREE_PROJECT: float = 0.1
_BASELINE_IN_DEGREE_OUTSIDE: float = 0.05


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_risk(
    parsed: ParsedCommand,
    resolutions: list[GraphResolution],
) -> RiskAssessment:
    """Combine parser output and graph resolution into a risk score.

    Formula: ``score = command_weight * normalized_in_degree * (1 / reversibility_factor)``

    Args:
        parsed: Output from ``parse_command()``.
        resolutions: Output from ``GraphResolver.resolve_paths()``.
                     May be empty if no graph data is available.

    Returns:
        A ``RiskAssessment`` with score, severity, rationale, and recommendation.

    Example::

        >>> score_risk(parse_command("rm -rf ./config"), [config_resolution])
        {"score": 0.9, "severity": "critical", ...}
    """
    command_weight = COMMAND_WEIGHTS.get(parsed["command"], DEFAULT_WEIGHT)

    # Aggregate in-degree from all resolutions
    total_in_degree = sum(r["in_degree"] for r in resolutions)
    has_graph_data = bool(resolutions) and any(
        r["nodes_in_file"] for r in resolutions
    )

    if has_graph_data:
        normalized_in_degree = min(total_in_degree / _IN_DEGREE_CEILING, 1.0)
        # Ensure non-zero if there are graph nodes but zero importers
        if normalized_in_degree == 0.0 and any(r["nodes_in_file"] for r in resolutions):
            normalized_in_degree = _BASELINE_IN_DEGREE_PROJECT
    else:
        # No graph data — use a baseline
        normalized_in_degree = _BASELINE_IN_DEGREE_PROJECT

    # Reversibility factor
    reversibility_factor = 1.0
    if parsed["reversible"]:
        reversibility_factor = 2.0
    if parsed["recursive"]:
        reversibility_factor *= 0.5

    # Raw score
    raw_score = command_weight * normalized_in_degree * (1.0 / reversibility_factor)
    score = max(0.0, min(1.0, raw_score))

    # Severity mapping
    severity = _score_to_severity(score)

    # Recommendation
    recommendation = _severity_to_recommendation(severity)

    # Collect all affected nodes
    affected_nodes: list[ResolvedNode] = []
    for r in resolutions:
        affected_nodes.extend(r["affected_nodes"])

    # Build rationale
    rationale = _build_rationale(parsed, resolutions, score, severity, has_graph_data)

    return RiskAssessment(
        score=round(score, 3),
        severity=severity,
        rationale=rationale,
        affected_nodes=affected_nodes,
        recommendation=recommendation,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _score_to_severity(score: float) -> str:
    """Map a 0.0-1.0 score to a severity level.

    Example::

        >>> _score_to_severity(0.85)
        "critical"
    """
    if score >= 0.8:
        return "critical"
    if score >= 0.5:
        return "high"
    if score >= 0.2:
        return "medium"
    return "low"


def _severity_to_recommendation(severity: str) -> str:
    """Map severity to a recommendation action.

    Example::

        >>> _severity_to_recommendation("critical")
        "block"
    """
    if severity == "critical":
        return "block"
    if severity in ("high", "medium"):
        return "confirm"
    return "proceed"


def _build_rationale(
    parsed: ParsedCommand,
    resolutions: list[GraphResolution],
    score: float,
    severity: str,
    has_graph_data: bool,
) -> str:
    """Build a human-readable explanation of the risk score.

    Example::

        >>> _build_rationale(...)
        "rm targets config.py which has 2 direct importers. Not git-tracked. Recursive. HIGH risk."
    """
    parts: list[str] = []

    # Command and targets
    if parsed["targets"]:
        target_names = [Path(t).name for t in parsed["targets"]]
        parts.append(f"{parsed['command']} targets {', '.join(target_names)}")
    else:
        parts.append(f"{parsed['command']} (no resolved targets)")

    # Graph information
    if has_graph_data:
        total_in = sum(r["in_degree"] for r in resolutions)
        total_affected = sum(r["total_affected"] for r in resolutions)
        parts.append(f"{total_in} direct importer(s), {total_affected} total affected")
    else:
        parts.append("no graph data available")

    # Reversibility
    if parsed["reversible"]:
        parts.append("git-tracked")
    else:
        parts.append("not git-tracked")

    # Recursive
    if parsed["recursive"]:
        parts.append("recursive deletion")

    # Severity tag
    parts.append(f"{severity.upper()} risk")

    return ". ".join(parts) + "."
