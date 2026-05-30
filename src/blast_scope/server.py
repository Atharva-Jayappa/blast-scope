"""MCP server entrypoint for blast-scope.

Exposes shell command risk assessment as MCP tools. This is the only
module with side effects — all scoring logic is delegated to pure functions.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from blast_scope import consequences as consequence_engine
from blast_scope import snapshot as snapshot_engine
from blast_scope.command_parser import (
    parse_command_chain,
    split_command_chain,
)
from blast_scope.graph_resolver import GraphResolver
from blast_scope.recoverability import Recoverability, classify_path, clear_cache
from blast_scope.risk_scorer import score_chain

logger = logging.getLogger(__name__)

mcp = FastMCP("blast-scope")

# Cache resolvers by project root so we don't rebuild the graph on every call
_resolvers: dict[str, GraphResolver] = {}
# Track which roots have been indexed in this server lifetime
_indexed_roots: set[str] = set()


def _get_resolver(project_root: Path, auto_index: bool = True) -> GraphResolver:
    """Get or create a cached GraphResolver for a project root.

    If ``auto_index`` is True (default), the graph is built automatically
    on first access for a given project root, or whenever the on-disk
    graph database is missing.

    Example::

        resolver = _get_resolver(Path("/home/user/project"))
    """
    key = str(project_root.resolve())
    if key not in _resolvers:
        _resolvers[key] = GraphResolver(project_root)

    resolver = _resolvers[key]

    if auto_index and key not in _indexed_roots:
        if not resolver._db_path.exists():
            logger.info("Auto-indexing project graph at %s", project_root)
            resolver.build_graph()
        _indexed_roots.add(key)

    return resolver


@mcp.tool()
def assess_command(
    command: str,
    cwd: str | None = None,
    project_root: str | None = None,
) -> dict:
    """Assess the blast radius of a shell command.

    Splits chained commands on ``&&``, ``||``, ``;``, and ``|``, then
    parses, resolves, and scores each segment independently. The top-level
    fields surface the worst step's score and recommendation; the ``chain``
    field contains every step's individual breakdown.

    When ``project_root`` is provided and no graph database exists yet,
    the graph is built automatically on the first call. Use
    ``index_project`` to force a rebuild.

    Args:
        command: Raw shell command string to analyze.
        cwd: Working directory for resolving relative paths.
             Defaults to the server's current working directory.
        project_root: Root directory of the project for graph-based scoring.
                      If provided, the graph is auto-built when missing.

    Returns:
        Structured risk assessment with worst-step score, severity,
        recommendation, and per-step chain breakdown.

    Example::

        assess_command("cd /tmp && rm -rf .", cwd="/home/user", project_root="/project")
    """
    return assess(command, cwd=cwd, project_root=project_root, auto_index=True)


def assess(
    command: str,
    cwd: str | None = None,
    project_root: str | None = None,
    auto_index: bool = True,
) -> dict:
    """Score a (possibly chained) shell command. Pure of MCP plumbing.

    Shared by the ``assess_command`` MCP tool and the PreToolUse hook. Graph
    scoring is used only when ``project_root`` is given; ``auto_index`` controls
    whether a missing graph is built (the tool builds it; the hook does not, to
    keep per-command latency low).

    Example::

        >>> assess("rm -rf ./config", cwd="/proj", project_root="/proj")["severity"]
        'critical'
    """
    working_dir = Path(cwd) if cwd else Path.cwd()
    segments = split_command_chain(command)
    parsed_list = parse_command_chain(command, cwd=working_dir)

    resolutions_per_command: list = []
    if project_root and (auto_index or _graph_exists(Path(project_root))):
        resolver = _get_resolver(Path(project_root), auto_index=auto_index)
        for parsed in parsed_list:
            resolutions_per_command.append(
                [resolver.resolve_path(Path(t)) for t in parsed["targets"]]
            )
    else:
        resolutions_per_command = [[] for _ in parsed_list]

    # Recoverability axis: per command, classify each target and keep the
    # worst (least recoverable) — that's what gates the safety of the step.
    recoverability_per_command = [
        _worst_recoverability(parsed["targets"]) for parsed in parsed_list
    ]

    # Out-of-graph consequences (VCS history, infra/deploy, config-by-path)
    # per command, gathered against the working dir and project root.
    root_path = Path(project_root) if project_root else None
    consequences_per_command = [
        consequence_engine.gather(parsed, segment, working_dir, root_path)
        for parsed, segment in zip(parsed_list, segments)
    ]

    assessment = score_chain(
        parsed_list,
        resolutions_per_command,
        raw_segments=segments,
        recoverability_per_command=recoverability_per_command,
        consequences_per_command=consequences_per_command,
    )

    return dict(assessment)


def _graph_exists(project_root: Path) -> bool:
    """True if a prebuilt graph DB exists for ``project_root`` (no build)."""
    return (project_root / ".blast-scope" / "graph.db").exists()


def _worst_recoverability(targets: list[str]) -> Recoverability | None:
    """Classify each target and return the least-recoverable one.

    Returns ``None`` when a command has no resolved targets, so the scorer
    falls back to the parser's coarse reversibility flag.

    Example::

        >>> _worst_recoverability(["/proj/.env", "/proj/README.md"])["category"]
        'secret'
    """
    worst: Recoverability | None = None
    for target in targets:
        rec = classify_path(Path(target))
        if worst is None or rec["irrecoverability"] > worst["irrecoverability"]:
            worst = rec
    return worst


@mcp.tool()
def index_project(project_root: str) -> dict:
    """Build or refresh the dependency graph for a project.

    Forces a graph rebuild for the given project root. Normally not
    required — ``assess_command`` auto-builds the graph on first use —
    but useful to refresh after a large code change.

    Args:
        project_root: Absolute path to the project root directory.

    Returns:
        Status dict confirming the project was indexed.

    Example::

        index_project("/home/user/my-project")
    """
    root = Path(project_root)
    # auto_index=False: we're about to do it explicitly
    resolver = _get_resolver(root, auto_index=False)
    resolver.build_graph(force=True)
    _indexed_roots.add(str(root.resolve()))
    # Git/recoverability state may have changed alongside the code — drop it.
    clear_cache()
    return {"status": "indexed", "project_root": str(root.resolve())}


@mcp.tool()
def list_snapshots(project_root: str) -> dict:
    """List undo snapshots captured before risky commands, newest first.

    Snapshots are taken automatically by the PreToolUse hook before a
    medium-or-higher risk command and stored under
    ``<project_root>/.blast-scope/snapshots``.

    Args:
        project_root: The project root the snapshots were taken under.

    Returns:
        ``{"snapshots": [{id, created, reason, paths}, ...]}``.

    Example::

        list_snapshots("/home/user/my-project")
    """
    snaps = snapshot_engine.list_snapshots(Path(project_root))
    return {
        "snapshots": [
            {
                "id": s["id"],
                "created": s["created"],
                "reason": s["reason"],
                "paths": [e["original"] for e in s["entries"]],
            }
            for s in snaps
        ]
    }


@mcp.tool()
def restore_snapshot(snapshot_id: str, project_root: str) -> dict:
    """Undo a risky command by restoring a snapshot's files in place.

    Overwrites whatever currently exists at each snapshotted path with the
    archived copy. Use ``list_snapshots`` to find the id.

    Args:
        snapshot_id: The snapshot id to restore.
        project_root: The project root the snapshot was taken under.

    Returns:
        ``{"status": "restored", "paths": [...]}`` or an ``error`` entry.

    Example::

        restore_snapshot("20260530T101500-a1b2c3", "/home/user/my-project")
    """
    try:
        restored = snapshot_engine.restore_snapshot(snapshot_id, root=Path(project_root))
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "restored", "snapshot_id": snapshot_id, "paths": restored}


def main() -> None:
    """Run the blast-scope MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
