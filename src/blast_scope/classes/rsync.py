"""Rsync consequence class — dry-run oracle for ``rsync --delete``.

``rsync --delete`` removes every file in the destination that the source
doesn't have — a target set that depends entirely on the two trees' current
state. rsync ships the perfect preview: ``--dry-run --itemize-changes``, whose
output the man page guarantees to match a subsequent real run ("barring
intentional trickery and system call failures"). Deletions appear as
``*deleting <path>`` lines.

Constraints honored here:

- **Local paths only.** A remote endpoint (``host:path`` / ``rsync://``) would
  make even the dry-run open a network connection (an auth event, a hang
  risk) — those degrade to a labeled estimate.
- The probe mirrors the real command's flags verbatim and only *adds*
  ``--dry-run --itemize-changes``.
- Missing rsync (stock Windows) or a failing dry-run degrades to an estimate;
  it is never treated as "safe".
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path

from blast_scope.classes import Candidate
from blast_scope.command_parser import ParsedCommand
from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 2.0
_MAX_TARGETS = 200
_DELETING_RE = re.compile(r"^\*deleting\s+(.+)$")
# host:path (single colon, not a Windows drive like C:\) or rsync:// URL.
_REMOTE_RE = re.compile(r"^(?:[^/:]+::|rsync://)|^[^/:\\]+:[^\\]")

_ESTIMATE_FLOOR = 0.4


class RsyncClass:
    """Consequence class for rsync --delete."""

    name = "rsync"

    def triage(self, raw: str, parsed: ParsedCommand) -> Candidate | None:
        """Match rsync invocations that carry a --delete variant.

        Example::

            >>> RsyncClass().triage("rsync -a --delete src/ dst/", p).operation
            'delete_sync'
        """
        if parsed.get("command") != "rsync":
            return None
        if not any(f.startswith("--delete") for f in parsed.get("flags", [])):
            return None
        return Candidate(cls=self.name, operation="delete_sync", raw=raw)

    def assess(self, candidate: Candidate, cwd: Path) -> Consequence | None:
        """Preview the deletion set via --dry-run --itemize-changes."""
        tokens = _tokens(candidate.raw)
        endpoints = [t for t in tokens[1:] if not t.startswith("-")]
        if any(_REMOTE_RE.match(e) for e in endpoints):
            return Consequence(
                "fs",
                _ESTIMATE_FLOOR,
                "rsync --delete against a remote endpoint — deletion set "
                "unverifiable without a network connection",
                estimated=True,
            )

        probe = list(tokens)
        if "--dry-run" not in probe and "-n" not in probe:
            probe.insert(1, "--dry-run")
        if "--itemize-changes" not in probe and "-i" not in probe:
            probe.insert(1, "--itemize-changes")
        out = _run_rsync(probe, cwd)
        if out is None:
            return Consequence(
                "fs",
                _ESTIMATE_FLOOR,
                "rsync --delete removes destination files missing from the "
                "source (dry-run unavailable — set unverified)",
                estimated=True,
            )

        dst = endpoints[-1] if endpoints else ""
        deleting: list[str] = []
        for line in out.splitlines():
            m = _DELETING_RE.match(line.strip())
            if m:
                rel = m.group(1).rstrip("/")
                deleting.append(str(Path(dst, rel)) if dst else rel)
            if len(deleting) >= _MAX_TARGETS:
                break

        if not deleting:
            return Consequence(
                "fs", 0.1, "rsync --delete dry-run: no destination files would be deleted"
            )
        floor = min(0.9, 0.4 + 0.05 * len(deleting))
        shown = ", ".join(Path(p).name for p in deleting[:6])
        if len(deleting) > 6:
            shown += "…"
        return Consequence(
            "fs",
            floor,
            f"rsync --delete would remove {len(deleting)} destination file(s) "
            f"(dry-run verified): {shown}",
            targets=tuple(deleting),
        )


def _tokens(raw: str) -> list[str]:
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _run_rsync(argv: list[str], cwd: Path) -> str | None:
    """Run the dry-run; None on missing binary, error, or timeout."""
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=_PROBE_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout
