"""PreToolUse hook: advisory blast-scope assessment + automatic snapshot.

Registered as a Claude Code ``PreToolUse`` hook on the ``Bash`` tool, this
reads the tool payload on stdin, scores the command, and — for anything
medium-risk or worse — captures an undo snapshot of the paths it would destroy.
It is **advisory**: it never blocks. The risk summary is returned as
``additionalContext`` so the agent sees the blast radius before proceeding, and
the snapshot id tells it how to undo if the call was a mistake.

Register in ``settings.json`` (see ``docs/hook.md``)::

    "hooks": {"PreToolUse": [{"matcher": "Bash",
      "hooks": [{"type": "command", "command": "python -m blast_scope.hook"}]}]}
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from blast_scope import snapshot as snapshot_engine
from blast_scope.server import assess

logger = logging.getLogger(__name__)

# Severities at or above which we capture an undo snapshot.
_SNAPSHOT_SEVERITIES: frozenset[str] = frozenset({"medium", "high", "critical"})


def run(payload: dict) -> dict:
    """Assess one PreToolUse payload; snapshot if risky; return hook output.

    Pure of stdin/stdout so it is unit-testable. Returns ``{}`` when there is
    nothing to say (non-Bash tool, empty command), which the caller maps to a
    silent allow.

    Args:
        payload: The PreToolUse JSON Claude Code sends (``tool_name``,
            ``tool_input.command``, ``cwd``).

    Returns:
        A hook-output dict with ``hookSpecificOutput.additionalContext``, or
        an empty dict to stay silent.

    Example::

        >>> run({"tool_name": "Bash", "tool_input": {"command": "rm -rf build"},
        ...      "cwd": "/proj"})["hookSpecificOutput"]["hookEventName"]
        'PreToolUse'
    """
    if payload.get("tool_name") not in (None, "Bash"):
        return {}
    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command.strip():
        return {}

    cwd = payload.get("cwd") or os.getcwd()
    project_root = os.environ.get("BLAST_SCOPE_PROJECT_ROOT") or cwd

    # auto_index=False: a hook must be fast — use the graph only if already built.
    try:
        assessment = assess(command, cwd=cwd, project_root=project_root, auto_index=False)
    except Exception:  # never let an analysis bug block the user's command
        logger.exception("blast-scope hook failed to assess %r", command)
        return {}

    snap = None
    if assessment["severity"] in _SNAPSHOT_SEVERITIES:
        targets = _destructive_targets(assessment)
        if targets:
            try:
                snap = snapshot_engine.create_snapshot(
                    targets, root=project_root, reason=command
                )
            except OSError:
                logger.exception("blast-scope hook failed to snapshot %r", targets)

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _format(assessment, snap),
        }
    }


def main() -> None:
    """Entry point: read PreToolUse JSON on stdin, emit hook output on stdout."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # malformed input — stay out of the way
    out = run(payload)
    if out:
        json.dump(out, sys.stdout)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _destructive_targets(assessment: dict) -> list[str]:
    """Collect existing-path targets of every destructive step in the chain."""
    targets: list[str] = []
    for step in assessment.get("chain", []):
        parsed = step.get("parsed", {})
        if parsed.get("intent") == "destructive":
            targets.extend(parsed.get("targets", []))
    return targets


def _format(assessment: dict, snap: dict | None) -> str:
    """Render the assessment (and any snapshot) as a compact advisory string."""
    sev = assessment["severity"].upper()
    score = assessment["score"]
    rec = assessment["recommendation"]
    lines = [f"[blast-scope] {sev} risk (score {score:.2f}) — recommendation: {rec}."]
    lines.append(assessment["rationale"])

    evidence = assessment.get("evidence") or []
    for item in evidence[:6]:
        lines.append(f"  • {item}")

    if snap is not None:
        paths = ", ".join(Path(e["original"]).name for e in snap["entries"])
        lines.append(
            f"Snapshot {snap['id']} saved ({paths}). "
            f"Undo with restore_snapshot(\"{snap['id']}\")."
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
