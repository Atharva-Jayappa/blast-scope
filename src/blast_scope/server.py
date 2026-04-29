"""MCP server entrypoint for blast-scope.

Exposes shell command risk assessment as MCP tools. This is the only
module with side effects — all scoring logic is delegated to pure functions.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from blast_scope.command_parser import (
    parse_command_chain,
    split_command_chain,
)
from blast_scope.graph_resolver import GraphResolver
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
    working_dir = Path(cwd) if cwd else Path.cwd()
    segments = split_command_chain(command)
    parsed_list = parse_command_chain(command, cwd=working_dir)

    resolutions_per_command: list = []
    if project_root:
        root = Path(project_root)
        resolver = _get_resolver(root, auto_index=True)
        for parsed in parsed_list:
            cmd_resolutions = []
            for target in parsed["targets"]:
                cmd_resolutions.append(resolver.resolve_path(Path(target)))
            resolutions_per_command.append(cmd_resolutions)
    else:
        resolutions_per_command = [[] for _ in parsed_list]

    assessment = score_chain(parsed_list, resolutions_per_command, raw_segments=segments)

    return dict(assessment)


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
    resolver.build_graph()
    _indexed_roots.add(str(root.resolve()))
    return {"status": "indexed", "project_root": str(root.resolve())}


def main() -> None:
    """Run the blast-scope MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
