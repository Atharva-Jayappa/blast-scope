"""PreToolUse hook: severity-tiered advisory + automatic snapshot.

Registered as a Claude Code ``PreToolUse`` hook on the ``Bash`` tool, this
reads the tool payload on stdin and scores the command. It is **advisory** —
it never blocks — and its volume scales with stakes so the rare loud message
keeps its signal:

- **low / medium** — silent. The common case stays quiet; constant low-value
  advice is what trains everyone to ignore the advisory entirely.
- **high** — surfaces the blast-radius assessment as ``additionalContext``.
- **critical** — surfaces the assessment *and* captures an undo snapshot of the
  paths the command would destroy, so a mistake is reversible.

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

# Severity → how loud the hook is. Friction scales with stakes: low/medium stay
# silent so the rare critical message isn't drowned out, high advises, and only
# critical also captures an undo snapshot.
_ADVISE_SEVERITIES: frozenset[str] = frozenset({"high", "critical"})
_SNAPSHOT_SEVERITIES: frozenset[str] = frozenset({"critical"})


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
        an empty dict to stay silent (non-Bash tool, empty command, or a
        low/medium-risk command that doesn't warrant interrupting the agent).

    Example::

        >>> run({"tool_name": "Bash", "tool_input": {"command": "rm -rf /etc"},
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

    severity = assessment["severity"]
    # Stay silent below the advise threshold — low/medium is the common case,
    # and surfacing it every time is exactly what trains the agent to tune out
    # the advisory it should heed on the rare critical command.
    if severity not in _ADVISE_SEVERITIES:
        return {}

    snap = None
    oversize: list[str] = []
    if severity in _SNAPSHOT_SEVERITIES:
        targets = _destructive_targets(assessment)
        if targets:
            # Decide what's worth/safe to archive before touching disk: skip
            # what git or a rebuild already covers, and don't tar oversize trees.
            plan = snapshot_engine.plan_snapshot(targets)
            oversize = plan["skipped_oversize"]
            if plan["archive"]:
                try:
                    snap = snapshot_engine.create_snapshot(
                        plan["archive"], root=project_root, reason=command
                    )
                except OSError:
                    logger.exception(
                        "blast-scope hook failed to snapshot %r", plan["archive"]
                    )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _format(assessment, snap, oversize),
        }
    }


# Cap stdin so a huge payload can't make the per-command hook allocate without
# bound. A real PreToolUse payload is a few KB; 8 MiB is generous headroom.
_MAX_STDIN_BYTES: int = 8 * 1024 * 1024


def main() -> None:
    """Entry point: read PreToolUse JSON on stdin, emit hook output on stdout."""
    try:
        payload = json.loads(sys.stdin.read(_MAX_STDIN_BYTES))
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


def _format(
    assessment: dict, snap: dict | None, oversize: list[str] | None = None
) -> str:
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

    if oversize:
        names = ", ".join(Path(p).name for p in oversize)
        lines.append(
            f"⚠ NOT snapshotted (too large to archive safely): {names}. "
            f"This deletion is not undoable via blast-scope — proceed with care."
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
