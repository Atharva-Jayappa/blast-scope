"""Consequence *classes* — per-command-class blast-radius analyzers.

This generalizes the proven consequence-analyzer pattern (``vcs`` / ``infra`` /
``config_refs``) into a registry of command *classes* (git, docker, pip/uv,
SQL), each running two stages so the common case stays cheap:

1. **triage** — a near-free check ("is this my class, and is it destructive?").
   Pure string/flag inspection, *no* subprocess. The vast majority of commands
   match nothing here and exit immediately.
2. **assess** — run only for triaged candidates. Performs a strictly
   side-effect-free *probe* when one is available, otherwise falls back to a
   labeled heuristic estimate. Either way it returns a :class:`Consequence`
   floor the scorer already knows how to consume.

The eligibility filter is structural: a class is only allowed a live probe when
both (a) a safe, side-effect-free read can observe the impact and (b) its
reversibility is authorable in a static table. Each class confines ``assess`` to
strictly side-effect-free reads by construction.

Adding a class = implement the protocol and list it in :func:`registry`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from blast_scope.command_parser import ParsedCommand
from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    """Stage-1 result: a destructive operation a class recognized in a command.

    Example::

        Candidate(cls="git", operation="reset_hard", raw="git reset --hard")
    """

    cls: str  # class name, e.g. "git"
    operation: str  # class-specific op id, e.g. "reset_hard" | "volume_rm"
    raw: str  # the original command segment (probes may re-derive operands)
    operands: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class ConsequenceClass(Protocol):
    """A command class that can triage and assess its destructive operations."""

    name: str

    def triage(self, raw: str, parsed: ParsedCommand) -> Candidate | None:
        """Cheaply decide if ``raw`` is a destructive op of this class.

        Must be near-free (no subprocess / network). Returns a
        :class:`Candidate` for destructive matches, else ``None``.
        """
        ...

    def assess(self, candidate: Candidate, cwd: Path) -> Consequence | None:
        """Probe (if safe + available) or estimate; return a floor consequence.

        Must never raise for an unavailable probe — degrade to a heuristic and
        mark the result ``estimated=True``.
        """
        ...


def registry() -> list[ConsequenceClass]:
    """Return the registered consequence classes.

    Imported lazily so the package stays import-cycle-free and classes that
    shell out are only loaded when consequence analysis actually runs.
    """
    from blast_scope.classes.docker import DockerClass
    from blast_scope.classes.find import FindClass
    from blast_scope.classes.git import GitClass
    from blast_scope.classes.packages import PackagesClass
    from blast_scope.classes.rsync import RsyncClass
    from blast_scope.classes.sql import SqlClass

    return [
        GitClass(),
        DockerClass(),
        PackagesClass(),
        SqlClass(),
        FindClass(),
        RsyncClass(),
    ]


def gather_classes(parsed: ParsedCommand, raw: str, cwd: Path | str) -> list[Consequence]:
    """Triage every class against one command; assess only the matches.

    This is the integration point ``consequences.gather`` calls. Triage is
    cheap and runs for all classes; the (potentially probing) ``assess`` runs
    only for triaged destructive candidates. Any class error degrades to
    "no consequence" — analysis is advisory and must never block a command.

    Example::

        >>> gather_classes(parse_command("git reset --hard"), "git reset --hard", cwd)
        [Consequence(domain='vcs', floor=0.6, ...)]
    """
    cwd_path = Path(cwd)
    out: list[Consequence] = []
    for cls in registry():
        try:
            candidate = cls.triage(raw, parsed)
        except Exception:  # a triage bug must never break assessment
            logger.debug("triage failed for class %s", getattr(cls, "name", cls), exc_info=True)
            continue
        if candidate is None:
            continue
        try:
            consequence = cls.assess(candidate, cwd_path)
        except Exception:  # probe/heuristic bug → degrade to silent for this class
            logger.debug("assess failed for %s", candidate, exc_info=True)
            continue
        if consequence is not None:
            out.append(consequence)
    return out
