"""Cross-domain consequence analysis — blast radius the code graph can't see.

The dependency graph only knows about code symbols (imports, calls, classes).
It is blind to three large classes of consequence:

- **VCS** — ``git reset --hard`` / ``git clean -fdx`` / ``git push --force``
  destroy uncommitted work or rewrite history. Their danger depends on the
  *current working-tree state*, not just the command syntax.
- **infra** — Dockerfiles, Terraform, k8s/helm manifests, CI configs: editing
  or deleting them has consequences in deployment environments, far beyond the
  repo's import graph.
- **config / data** — ``config.yaml`` / ``.env`` / ``seed.json`` are loaded by
  *path string* at runtime, so no AST edge points at them; deleting one breaks
  code that the graph says depends on nothing.

Each analyzer returns zero or more :class:`Consequence` records. A consequence
can only *raise* a score (it expresses "this is worse than the code graph
suggests"), never lower it — so it is applied as a floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle at module load
    from blast_scope.command_parser import ParsedCommand


@dataclass(frozen=True)
class Consequence:
    """A single out-of-graph consequence of a command.

    Example::

        Consequence(domain="vcs", floor=0.7,
                    evidence="git reset --hard would discard 4 modified file(s)")
    """

    domain: str  # "vcs" | "infra" | "config" | "docker" | "packages" | "sql"
    floor: float  # minimum score this consequence justifies, 0.0 - 1.0
    evidence: str  # human-readable explanation
    estimated: bool = False  # True when derived from a heuristic, not a live probe


def gather(
    parsed: ParsedCommand,
    raw: str,
    cwd: Path,
    project_root: Path | None = None,
) -> list[Consequence]:
    """Run every domain analyzer for a single parsed command.

    Args:
        parsed: Output of ``parse_command()``.
        raw: The original command string for this segment (the git analyzer
            needs the subcommand, which the parser does not retain).
        cwd: Working directory the command runs in.
        project_root: Project root for the config-reference scan, if known.

    Returns:
        All consequences found, across VCS / infra / config domains.

    Example::

        >>> gather(parse_command("git reset --hard"), "git reset --hard", cwd)
        [Consequence(domain='vcs', ...)]
    """
    # Local imports keep this module a dependency-cycle-free leaf: the
    # analyzers import ``Consequence`` from here.
    from blast_scope import config_refs, infra
    from blast_scope.classes import gather_classes

    out: list[Consequence] = []

    # Command-class analyzers (git, and — as they land — docker/packages/sql):
    # each triages cheaply, then probes only flagged destructive candidates.
    out.extend(gather_classes(parsed, raw, cwd))

    for target in parsed["targets"]:
        path = Path(target)
        infra_c = infra.classify_infra(path)
        if infra_c is not None:
            out.append(infra_c)
        config_c = config_refs.analyze_config_refs(path, project_root)
        if config_c is not None:
            out.append(config_c)

    return out


def max_floor(consequences: list[Consequence]) -> float:
    """Return the strongest floor among consequences (0.0 if none).

    Example::

        >>> max_floor([Consequence("vcs", 0.7, ""), Consequence("infra", 0.6, "")])
        0.7
    """
    return max((c.floor for c in consequences), default=0.0)
