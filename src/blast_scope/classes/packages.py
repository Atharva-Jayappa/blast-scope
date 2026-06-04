"""Package-manager consequence class — pip / uv uninstalls.

A Python environment is the most *recoverable* thing in this set: if a lockfile
or requirements manifest is present, uninstalling a package is fully regenerable
(``uv sync`` / ``pip install -r`` restores exact versions). Without one, the
exact version/extras may not reproduce, so the risk is a notch higher.

This class's "probe" is pure, always-available filesystem reads (no subprocess),
so it never degrades to an estimate. Deleting ``.venv`` itself is deliberately
*not* handled here — :mod:`blast_scope.recoverability` already classifies it
``regenerable`` (capped low), and double-scoring it would fight that cap.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

from blast_scope.classes import Candidate
from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)

# Manifests that make an environment reproducible, best (lock) first.
_LOCKFILES: tuple[str, ...] = (
    "uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock",
    "requirements.txt", "requirements.lock",
)


class PackagesClass:
    """Consequence class for pip / uv package removals."""

    name = "packages"

    # -- Stage 1: triage -----------------------------------------------------

    def triage(self, raw: str, parsed) -> Candidate | None:
        """Recognize ``pip uninstall`` / ``uv pip uninstall`` cheaply."""
        cmd = parsed.get("command")
        tokens = _tokens(raw)
        if cmd in ("pip", "pip3") and "uninstall" in tokens:
            pkgs = _packages_after(tokens, "uninstall")
            return Candidate(self.name, "pip_uninstall", raw, operands=pkgs)
        if cmd == "uv" and tokens[1:3] == ["pip", "uninstall"]:
            pkgs = _packages_after(tokens, "uninstall")
            return Candidate(self.name, "uv_uninstall", raw, operands=pkgs)
        return None

    # -- probe surface: none external (pure file reads) ----------------------

    def probe_commands(self, candidate: Candidate) -> list[list[str]]:
        """No external probe — recoverability is read from manifest files."""
        return []

    # -- Stage 2: assess -----------------------------------------------------

    def assess(self, candidate: Candidate, cwd: Path) -> Consequence | None:
        """Floor depends on whether the env is reproducible from a lockfile."""
        pkgs = ", ".join(candidate.operands) or "package(s)"
        lockfile = _find_lockfile(cwd)
        if lockfile is not None:
            return Consequence(
                "packages", 0.15,
                f"uninstalling {pkgs} — regenerable from {lockfile} "
                f"(reinstall restores the exact versions)",
            )
        return Consequence(
            "packages", 0.35,
            f"uninstalling {pkgs} — no lockfile/requirements found; a reinstall "
            f"may not reproduce the exact version or extras",
        )


# ---------------------------------------------------------------------------
# Helpers (pure)
# ---------------------------------------------------------------------------


def _tokens(raw: str) -> list[str]:
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _packages_after(tokens: list[str], keyword: str) -> tuple[str, ...]:
    """Non-flag operands after ``keyword`` (the package names)."""
    if keyword not in tokens:
        return ()
    after = tokens[tokens.index(keyword) + 1 :]
    return tuple(t for t in after if not t.startswith("-"))


def _find_lockfile(cwd: Path) -> str | None:
    """Return the first lockfile/manifest present in ``cwd`` (project root)."""
    for name in _LOCKFILES:
        if (cwd / name).is_file():
            return name
    return None
