"""MCP server entrypoint for blast-scope.

Exposes shell command risk assessment as MCP tools. This is the only
module with side effects — all scoring logic is delegated to pure functions.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from blast_scope.command_parser import parse_command
from blast_scope.graph_resolver import GraphResolver
from blast_scope.risk_scorer import score_risk

logger = logging.getLogger(__name__)

mcp = FastMCP("blast-scope")

# Cache resolvers by project root so we don't rebuild the graph on every call
_resolvers: dict[str, GraphResolver] = {}


def _get_resolver(project_root: Path) -> GraphResolver:
    """Get or create a cached GraphResolver for a project root.

    Example::

        resolver = _get_resolver(Path("/home/user/project"))
    """
    key = str(project_root.resolve())
    if key not in _resolvers:
        _resolvers[key] = GraphResolver(project_root)
    return _resolvers[key]


@mcp.tool()
def assess_command(
    command: str,
    cwd: str | None = None,
    project_root: str | None = None,
) -> dict:
    """Assess the blast radius of a shell command.

    Parses the command, resolves target paths against the dependency graph,
    and returns a structured risk assessment with a score from 0.0 to 1.0.

    Args:
        command: Raw shell command string to analyze.
        cwd: Working directory for resolving relative paths.
             Defaults to the server's current working directory.
        project_root: Root directory of the project for graph-based scoring.
                      If provided and the project has been indexed, the
                      assessment includes dependency-aware risk scoring.

    Returns:
        Structured risk assessment including parsed command, risk score,
        severity level, and recommendation.

    Example::

        assess_command("rm -rf ./config", cwd="/project", project_root="/project")
    """
    working_dir = Path(cwd) if cwd else Path.cwd()
    parsed = parse_command(command, cwd=working_dir)

    resolutions = []
    if project_root:
        root = Path(project_root)
        resolver = _get_resolver(root)
        for target in parsed["targets"]:
            resolution = resolver.resolve_path(Path(target))
            resolutions.append(resolution)

    assessment = score_risk(parsed, resolutions)

    return {
        "parsed": dict(parsed),
        **assessment,
    }


@mcp.tool()
def index_project(project_root: str) -> dict:
    """Build or refresh the dependency graph for a project.

    Call this once before using assess_command with a project_root to enable
    graph-based risk scoring. The graph is cached — subsequent calls to
    assess_command will reuse the built graph.

    Args:
        project_root: Absolute path to the project root directory.

    Returns:
        Status dict confirming the project was indexed.

    Example::

        index_project("/home/user/my-project")
    """
    root = Path(project_root)
    resolver = _get_resolver(root)
    resolver.build_graph()
    return {"status": "indexed", "project_root": str(root.resolve())}


def main() -> None:
    """Run the blast-scope MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
