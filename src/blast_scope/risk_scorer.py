"""Combine parser output and graph resolution into a risk score.

Pure functions — no side effects. Takes structured data from the command
parser and graph resolver and produces a human-readable risk assessment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from blast_scope.command_parser import ParsedCommand
from blast_scope.consequences import Consequence
from blast_scope.graph_resolver import GraphResolution, ResolvedNode
from blast_scope.recoverability import Recoverability

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
    recoverability: str  # category from recoverability.classify_path, or "unknown"
    evidence: list[str]  # short, human-readable reasons behind the score


class ChainStep(TypedDict):
    """A single command in a chained shell expression with its own risk.

    Example::

        {
            "command": "rm -rf .",
            "parsed": {...},
            "assessment": {...},
        }
    """

    command: str
    parsed: ParsedCommand
    assessment: RiskAssessment


class ChainAssessment(TypedDict):
    """Risk assessment for a chained command, with per-step breakdown.

    The top-level ``score`` / ``severity`` / ``recommendation`` fields
    reflect the worst single step in the chain. The ``chain`` field
    contains every step's individual assessment.

    Example::

        {
            "score": 0.9,
            "severity": "critical",
            "rationale": "Chain of 2 commands. Worst: rm ...",
            "affected_nodes": [...],
            "recommendation": "block",
            "chain": [{...}, {...}],
        }
    """

    score: float
    severity: str
    rationale: str
    affected_nodes: list[ResolvedNode]
    recommendation: str
    recoverability: str
    evidence: list[str]
    chain: list[ChainStep]


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

# Consequence domains whose danger lives in *state*, not in a filesystem path
# (git working tree, a docker volume, a package env, a SQL table). Their floors
# apply AFTER the recoverability caps and ungated, because the command's operand
# isn't a path the caps reason about — otherwise the bogus `absent` cap (from
# treating e.g. a git subcommand token as a missing file) would crush a real
# consequence. Path-tied domains (infra/config) stay BEFORE the caps.
_STATE_TIED_DOMAINS: frozenset[str] = frozenset({"vcs", "docker", "packages", "sql"})

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
    recoverability: Recoverability | None = None,
    consequences: list[Consequence] | None = None,
) -> RiskAssessment:
    """Combine parser output, graph resolution, and recoverability into a score.

    Two orthogonal axes drive the result:

    - **blast radius** — ``command_weight × normalized_in_degree``: how much
      depends on the target and how dangerous the verb is.
    - **reversibility** — can the change be undone? When ``recoverability`` is
      supplied it drives this axis (git history, regenerable artifacts, secrets);
      otherwise the parser's coarse ``reversible`` flag is used.

    Formula: ``score = command_weight × normalized_in_degree × (1 / reversibility_factor)``,
    then category floors/caps are applied (a secret is high-risk even with zero
    importers; a regenerable artifact stays low even if widely imported).

    Args:
        parsed: Output from ``parse_command()``.
        resolutions: Output from ``GraphResolver.resolve_paths()``.
                     May be empty if no graph data is available.
        recoverability: Optional worst-case recoverability of the targets,
                     from ``recoverability.classify_path``. When omitted the
                     scorer falls back to the parser's ``reversible`` flag.
        consequences: Optional out-of-graph consequences (VCS/infra/config)
                     from ``consequences.gather``. Each raises the score to at
                     least its floor — applied before recoverability caps, so a
                     regenerable/absent target still caps low.

    Returns:
        A ``RiskAssessment`` with score, severity, rationale, and recommendation.

    Example::

        >>> score_risk(parse_command("rm -rf ./config"), [config_resolution])
        {"score": 0.9, "severity": "critical", ...}
    """
    weight = parsed["weight"]

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

    # Structural blast-radius signal. Raw in-degree is a local count; PageRank
    # importance is a global, edge-type-weighted centrality. A file is
    # high-blast-radius if EITHER many things import it directly OR it is
    # globally central — so take the stronger of the two signals.
    importance = max((r.get("importance", 0.0) for r in resolutions), default=0.0)
    structural = max(normalized_in_degree, importance)

    # Reversibility axis. With recoverability data, irrecoverability scales the
    # factor continuously: fully recoverable (irr 0) halves risk like the old
    # `reversible` flag; gone-for-good (irr 1) multiplies it ~4×.
    if recoverability is not None:
        irr = recoverability["irrecoverability"]
        reversibility_factor = max(0.25, 2.0 - 1.75 * irr)
    else:
        reversibility_factor = 2.0 if parsed["reversible"] else 1.0
    if parsed["recursive"]:
        reversibility_factor *= 0.5

    # Raw score
    raw_score = weight * structural * (1.0 / reversibility_factor)
    score = max(0.0, min(1.0, raw_score))

    # Path-tied consequences (infra/deploy reach, config files loaded by path)
    # raise the score to their floor. Applied BEFORE the recoverability caps
    # below so a regenerable/absent target — node_modules, a path that doesn't
    # exist — still caps low even if something references it. Gated on
    # *destructive* intent: the floor expresses "destroying/overwriting this
    # config or infra file breaks things the graph can't see", so a read or a
    # plain copy (`hexdump config.json`, `cp config.toml dest`) that merely names
    # the file must not inherit it — that was a false-positive source on SABER.
    if consequences and parsed["intent"] == "destructive":
        path_floor = max(
            (c.floor for c in consequences if c.domain not in _STATE_TIED_DOMAINS),
            default=0.0,
        )
        score = max(score, path_floor)

    # Category floors/caps — the reversibility axis on its own can't express
    # "irreplaceable regardless of importers" or "always cheap to rebuild".
    # Only applied to commands that actually change state (weight > 0).
    if recoverability is not None and weight > 0.0:
        cat = recoverability["category"]
        # FLOORS ("losing this is catastrophic") answer *how bad if the target is
        # destroyed* — so they apply only to a genuinely destructive op. An
        # `unknown`-intent command that merely *names* a sensitive file
        # (`sqlite3 app.db '.tables'`, `hexdump key.pem`, `python3 run.py`) reads
        # or executes it; it must not inherit a deletion's blast radius. Gating
        # on `weight > 0` alone made these the dominant false-positive source on
        # the SABER corpus. CAPS only ever *lower* the score, so they stay
        # ungated by intent (a read of node_modules is cheap regardless).
        # chmod/chown change *metadata*, not content — `chmod 600 id_rsa` hardens
        # a key, it doesn't destroy it — so they don't inherit a content floor.
        destroys = (
            parsed["intent"] == "destructive"
            and parsed["command"] not in ("chmod", "chown")
        )
        if cat == "absent":
            score = min(score, 0.1)
        elif cat == "regenerable":
            score = min(score, 0.15)
        elif destroys and cat == "secret":
            score = max(score, 0.85)
        elif destroys and cat == "repo_history":
            # Deleting .git / the repo root takes down the recovery net itself.
            score = max(score, 0.7)
        elif destroys and cat in ("precious_data", "gitignored"):
            score = max(score, 0.6)
        elif destroys and cat == "untracked":
            # Not in git history → unrecoverable once destroyed. Even an
            # unknown file is at least a medium concern when it's gone for good.
            score = max(score, 0.2)

    # State-tied consequences (git working tree, docker volumes, package envs,
    # SQL tables) are orthogonal to the target *path* — the danger is in runtime
    # state, not the operand (git's "targets" are subcommands/refs, not files).
    # So apply them AFTER the recoverability caps, and ungated: each class only
    # emits a consequence for a genuinely destructive op, never for a read.
    if consequences:
        state_floor = max(
            (c.floor for c in consequences if c.domain in _STATE_TIED_DOMAINS),
            default=0.0,
        )
        score = max(score, state_floor)

    # Severity mapping
    severity = _score_to_severity(score)

    # Recommendation
    recommendation = _severity_to_recommendation(severity)

    # Collect all affected nodes
    affected_nodes: list[ResolvedNode] = []
    for r in resolutions:
        affected_nodes.extend(r["affected_nodes"])

    # Build rationale + evidence. For state-tied classes (git/docker/sql/
    # packages) the operands the parser saw are subcommands/refs/table names,
    # not filesystem paths — so the "targets" are bogus paths and the recover-
    # ability/rationale derived from them is misleading noise ("path does not
    # exist — nothing to lose" on `git reset --hard`). When such a consequence
    # is present, lead the explanation with IT rather than the path model.
    state_consequences = (
        [c for c in consequences if c.domain in _STATE_TIED_DOMAINS]
        if consequences
        else []
    )
    if state_consequences:
        lead = max(state_consequences, key=lambda c: c.floor)
        rationale = f"{lead.evidence}. {severity.upper()} risk."
        evidence = [c.evidence for c in consequences]
    else:
        rationale = _build_rationale(parsed, resolutions, score, severity, has_graph_data)
        evidence = _build_evidence(
            parsed, resolutions, recoverability, has_graph_data, importance
        )
        if consequences and parsed["intent"] != "read":
            evidence.extend(c.evidence for c in consequences)

    return RiskAssessment(
        score=round(score, 3),
        severity=severity,
        rationale=rationale,
        affected_nodes=affected_nodes,
        recommendation=recommendation,
        recoverability=recoverability["category"] if recoverability else "unknown",
        evidence=evidence,
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


def _build_evidence(
    parsed: ParsedCommand,
    resolutions: list[GraphResolution],
    recoverability: Recoverability | None,
    has_graph_data: bool,
    importance: float = 0.0,
) -> list[str]:
    """Collect the discrete signals behind a score, as short strings.

    Unlike the prose ``rationale``, this is a machine-friendly list an agent
    or UI can render as bullet points.

    Example::

        >>> _build_evidence(parsed, resolutions, recoverability, True, 0.9)
        ["3 importer(s), 5 affected node(s)", "high centrality ...", "git-tracked ..."]
    """
    evidence: list[str] = []

    if has_graph_data:
        total_in = sum(r["in_degree"] for r in resolutions)
        total_affected = sum(r["total_affected"] for r in resolutions)
        evidence.append(f"{total_in} importer(s), {total_affected} affected node(s)")

    if importance >= 0.5:
        evidence.append(
            f"high centrality (PageRank {importance:.2f}) — a hub other code routes through"
        )

    if recoverability is not None:
        evidence.append(recoverability["reason"])

    if parsed["recursive"]:
        evidence.append("recursive — applies to every file underneath")

    return evidence


# ---------------------------------------------------------------------------
# Chain scoring
# ---------------------------------------------------------------------------


_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


def score_chain(
    parsed_list: list[ParsedCommand],
    resolutions_per_command: list[list[GraphResolution]],
    raw_segments: list[str] | None = None,
    recoverability_per_command: list[Recoverability | None] | None = None,
    consequences_per_command: list[list[Consequence] | None] | None = None,
) -> ChainAssessment:
    """Score a chained shell command, returning the worst-step assessment.

    Each command in the chain is scored independently. The top-level fields
    surface the worst step's score and recommendation, on the principle that
    a chain is only as safe as its riskiest link.

    Args:
        parsed_list: Output of ``parse_command_chain()``.
        resolutions_per_command: One list of ``GraphResolution`` per parsed
            command, in the same order. Pass ``[]`` for commands with no
            graph data.
        raw_segments: Optional original string for each segment (for the
            chain breakdown). If omitted, the parsed command name is used.

    Returns:
        A ``ChainAssessment`` with the worst step elevated to the top level
        and every step's individual assessment in ``chain``.

    Example::

        >>> score_chain(parse_command_chain("cd /tmp && rm -rf ."), [[], [...]])
        {"score": 0.9, "severity": "critical", "chain": [...], ...}
    """
    if not parsed_list:
        return ChainAssessment(
            score=0.0,
            severity="low",
            rationale="empty command",
            affected_nodes=[],
            recommendation="proceed",
            recoverability="unknown",
            evidence=[],
            chain=[],
        )

    if len(resolutions_per_command) != len(parsed_list):
        # Pad with empty resolutions if mismatched
        resolutions_per_command = list(resolutions_per_command) + [
            [] for _ in range(len(parsed_list) - len(resolutions_per_command))
        ]

    if recoverability_per_command is None or len(recoverability_per_command) != len(parsed_list):
        recoverability_per_command = [None] * len(parsed_list)

    if consequences_per_command is None or len(consequences_per_command) != len(parsed_list):
        consequences_per_command = [None] * len(parsed_list)

    if raw_segments is None or len(raw_segments) != len(parsed_list):
        raw_segments = [p["command"] for p in parsed_list]

    chain: list[ChainStep] = []
    worst_idx = 0
    worst_rank = -1
    worst_score = -1.0
    all_affected: list[ResolvedNode] = []

    for i, (parsed, resolutions) in enumerate(zip(parsed_list, resolutions_per_command)):
        assessment = score_risk(
            parsed,
            resolutions,
            recoverability_per_command[i],
            consequences_per_command[i],
        )
        chain.append(
            ChainStep(
                command=raw_segments[i],
                parsed=parsed,
                assessment=assessment,
            )
        )
        all_affected.extend(assessment["affected_nodes"])

        rank = _SEVERITY_RANK.get(assessment["severity"], 0)
        if rank > worst_rank or (rank == worst_rank and assessment["score"] > worst_score):
            worst_rank = rank
            worst_score = assessment["score"]
            worst_idx = i

    worst = chain[worst_idx]["assessment"]

    if len(chain) == 1:
        rationale = worst["rationale"]
    else:
        rationale = (
            f"Chain of {len(chain)} commands. Worst step ({worst_idx + 1}/{len(chain)}): "
            f"{worst['rationale']}"
        )

    # Deduplicate affected nodes by qualified_name
    seen: set[str] = set()
    deduped: list[ResolvedNode] = []
    for node in all_affected:
        qn = node["qualified_name"]
        if qn not in seen:
            seen.add(qn)
            deduped.append(node)

    return ChainAssessment(
        score=worst["score"],
        severity=worst["severity"],
        rationale=rationale,
        affected_nodes=deduped,
        recommendation=worst["recommendation"],
        recoverability=worst["recoverability"],
        evidence=worst["evidence"],
        chain=chain,
    )
