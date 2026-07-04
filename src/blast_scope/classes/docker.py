"""Docker consequence class — container/volume blast radius.

Docker's destructive verbs split cleanly along reversibility:

- **volumes** carry data with *no image to rebuild from* — ``docker volume rm``
  or a ``system prune --volumes`` is irreversible.
- **containers / images** are recreatable: ``docker rm -f`` drops a container
  that its image can recreate; ``system prune -a`` removes images that are
  re-pullable (a slow rebuild, not a loss).

The safe probe is the read-only daemon API (``volume inspect`` / ``ps`` /
``volume ls`` / ``images``). When the docker CLI is missing or the daemon is
unreachable, the class degrades to a heuristic floor and labels it ``estimated``.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from blast_scope.classes import Candidate
from blast_scope.command_parser import ParsedCommand
from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 3.0
_DOCKER_OBJECTS = frozenset({"volume", "system", "container", "image", "network"})


class DockerClass:
    """Consequence class for destructive docker operations."""

    name = "docker"

    # -- Stage 1: triage -----------------------------------------------------

    def triage(self, raw: str, parsed: ParsedCommand) -> Candidate | None:
        """Recognize ``docker volume rm`` / ``system prune`` / ``rm -f`` cheaply."""
        if parsed.get("command") != "docker":
            return None
        spec = _parse_docker(raw)
        if spec is None:
            return None
        op = _docker_op(spec)
        if op is None:
            return None
        operation, operands = op
        return Candidate(cls=self.name, operation=operation, raw=raw, operands=operands)

    # -- Stage 2: assess -----------------------------------------------------

    def assess(self, candidate: Candidate, cwd: Path) -> Consequence | None:
        """Probe the daemon (if reachable) or estimate; return a floor."""
        daemon = _daemon_up(cwd)
        op = candidate.operation
        if op == "volume_rm":
            return _assess_volume_rm(candidate.operands, cwd, daemon)
        if op == "volume_prune":
            return _assess_volume_prune(cwd, daemon)
        if op == "system_prune":
            return _assess_system_prune(candidate.raw, cwd, daemon)
        if op == "container_rm":
            return _assess_container_rm(candidate.operands, cwd, daemon)
        return None


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def _parse_docker(raw: str) -> tuple[str, str, list[str], list[str]] | None:
    """Split a docker command into (object, action, args, flags)."""
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if not tokens:
        return None
    if tokens[0] == "sudo" and len(tokens) >= 2 and tokens[1] == "docker":
        tokens = tokens[1:]
    if not tokens or tokens[0] != "docker":
        return None

    rest = tokens[1:]
    flags = [t for t in rest if t.startswith("-")]
    positional = [t for t in rest if not t.startswith("-")]
    if not positional:
        return None

    if positional[0] in _DOCKER_OBJECTS:
        obj = positional[0]
        action = positional[1] if len(positional) > 1 else ""
        args = positional[2:]
    else:
        obj = ""
        action = positional[0]
        args = positional[1:]
    return obj, action, args, flags


def _docker_op(spec: tuple[str, str, list[str], list[str]]) -> tuple[str, tuple[str, ...]] | None:
    """Map a parsed docker command to a destructive operation id, or ``None``."""
    obj, action, args, flags = spec
    force = any(f in ("-f", "--force") for f in flags)
    if obj == "volume" and action == "rm":
        return ("volume_rm", tuple(args))
    if obj == "volume" and action == "prune":
        return ("volume_prune", ())
    if obj == "system" and action == "prune":
        return ("system_prune", ())
    if action == "rm" and force and obj in ("", "container"):
        return ("container_rm", tuple(args))
    return None


# ---------------------------------------------------------------------------
# Probe helpers (read-only)
# ---------------------------------------------------------------------------


def _run_docker(cwd: Path, *args: str) -> tuple[bool, str]:
    """Run a read-only docker command → (succeeded, stdout). Never raises."""
    try:
        result = subprocess.run(
            ["docker", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return (False, "")
    return (result.returncode == 0, result.stdout.strip())


def _daemon_up(cwd: Path) -> bool:
    """True if the docker CLI exists and the daemon answers a read-only call."""
    return _run_docker(cwd, "volume", "ls", "-q")[0]


def _count_lines(out: str) -> int:
    return len([ln for ln in out.splitlines() if ln.strip()])


# ---------------------------------------------------------------------------
# Per-operation assessment
# ---------------------------------------------------------------------------


def _assess_volume_rm(volumes: tuple[str, ...], cwd: Path, daemon: bool) -> Consequence:
    names = ", ".join(volumes) or "volume(s)"
    if not daemon:
        return Consequence(
            "docker", 0.7,
            f"removing docker volume(s) {names} — if they hold data this is "
            f"irreversible (daemon unreachable, could not confirm)",
            estimated=True,
        )

    existing: list[str] = []
    in_use: list[str] = []
    for v in volumes:
        ok, out = _run_docker(cwd, "volume", "inspect", v)
        if ok and out:
            existing.append(v)
            users_ok, users = _run_docker(
                cwd, "ps", "-a", "--filter", f"volume={v}", "--format", "{{.Names}}"
            )
            if users_ok and users:
                in_use.append(v)

    if not existing:
        return Consequence(
            "docker", 0.1,
            f"volume(s) {names} do not exist — nothing to remove",
        )
    floor = 0.9 if in_use else 0.85
    suffix = f" (in use by {', '.join(in_use)})" if in_use else ""
    return Consequence(
        "docker", floor,
        f"removing volume(s) {', '.join(existing)} permanently deletes their "
        f"data{suffix} — a volume has no image to rebuild from",
    )


def _assess_volume_prune(cwd: Path, daemon: bool) -> Consequence:
    if not daemon:
        return Consequence(
            "docker", 0.6,
            "volume prune removes all unused volumes and their data — "
            "irreversible (daemon unreachable)",
            estimated=True,
        )
    ok, out = _run_docker(cwd, "volume", "ls", "-f", "dangling=true", "-q")
    n = _count_lines(out) if ok else 0
    if n == 0:
        return Consequence("docker", 0.15, "no unused volumes to prune")
    floor = min(0.85, 0.5 + 0.05 * n)
    return Consequence(
        "docker", floor,
        f"volume prune would delete {n} unused volume(s) and their data (irreversible)",
    )


def _assess_system_prune(raw: str, cwd: Path, daemon: bool) -> Consequence:
    spec = _parse_docker(raw)
    flags = spec[3] if spec else []
    has_volumes = "--volumes" in flags
    has_all = any(f in ("-a", "--all") for f in flags)

    if not daemon:
        floor = 0.75 if has_volumes else (0.5 if has_all else 0.4)
        extra = " including volumes and their data" if has_volumes else ""
        return Consequence(
            "docker", floor,
            f"system prune removes all unused docker objects{extra} — "
            f"images are re-pullable, volume data is not (daemon unreachable)",
            estimated=True,
        )

    if has_volumes:
        ok, out = _run_docker(cwd, "volume", "ls", "-f", "dangling=true", "-q")
        nv = _count_lines(out) if ok else 0
        if nv > 0:
            return Consequence(
                "docker", min(0.85, 0.6 + 0.05 * nv),
                f"system prune --volumes would delete {nv} unused volume(s) and "
                f"their data — irreversible",
            )
        return Consequence(
            "docker", 0.4,
            "system prune --volumes — no unused volumes; removes containers/images "
            "(rebuildable)",
        )
    if has_all:
        return Consequence(
            "docker", 0.5,
            "system prune -a removes every unused image — re-pullable, but a slow "
            "rebuild; no volume data is touched",
        )
    return Consequence(
        "docker", 0.4,
        "system prune removes stopped containers, dangling images and build cache "
        "— all rebuildable",
    )


def _assess_container_rm(containers: tuple[str, ...], cwd: Path, daemon: bool) -> Consequence:
    names = ", ".join(containers) or "container(s)"
    evidence = (
        f"force-removes container(s) {names}; recreatable from their image "
        f"(any in-container state not on a volume is lost)"
    )
    if not daemon:
        return Consequence("docker", 0.4, evidence + " (daemon unreachable)", estimated=True)
    return Consequence("docker", 0.4, evidence)
