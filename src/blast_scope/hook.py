"""PreToolUse + SessionStart hook: severity-tiered advisory + automatic snapshot.

Registered as a Claude Code hook, this reads the payload on stdin and
dispatches on the event. On ``PreToolUse`` (``Bash`` tool) it scores the
command. It is **advisory** — it never blocks — and its volume scales with
stakes so the rare loud message keeps its signal:

- **low / medium** — silent. The common case stays quiet; constant low-value
  advice is what trains everyone to ignore the advisory entirely.
- **high** — surfaces the blast-radius assessment as ``additionalContext``.
- **critical** — surfaces the assessment *and* captures an undo snapshot of the
  paths the command would destroy, so a mistake is reversible.

Graph freshness rides on the same two events (see :mod:`blast_scope.indexing`):
``SessionStart`` kicks off a detached cold index so the graph exists without
anyone calling the MCP server, and each ``PreToolUse`` refreshes it
incrementally before scoring so mid-session edits can't stale the verdict.

Register in ``settings.json`` (see ``docs/hook.md``)::

    "hooks": {
      "SessionStart": [{"hooks":
        [{"type": "command", "command": "python -m blast_scope.hook"}]}],
      "PreToolUse": [{"matcher": "Bash",
        "hooks": [{"type": "command", "command": "python -m blast_scope.hook"}]}]}
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from blast_scope import indexing
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

    # Keep the graph honest before scoring: incremental refresh when one
    # exists (a stat sweep when nothing changed), detached cold build when
    # none does. Non-blocking and never raises — scoring proceeds either way.
    indexing.ensure_fresh_graph(Path(project_root))

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


def run_session_start(payload: dict) -> dict:
    """Kick off a detached background graph build for the session's project.

    Returns ``{}`` always — session start has nothing to tell the agent; the
    build (if one was needed) runs off the command path so the first scored
    commands find a graph waiting.

    Example::

        >>> run_session_start({"cwd": "/proj"})
        {}
    """
    cwd = payload.get("cwd") or os.getcwd()
    project_root = os.environ.get("BLAST_SCOPE_PROJECT_ROOT") or cwd
    indexing.spawn_background_build(Path(project_root))
    return {}


def main() -> None:
    """Entry point: read hook JSON on stdin, dispatch on event, emit output."""
    try:
        payload = json.loads(sys.stdin.read(_MAX_STDIN_BYTES))
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # malformed input — stay out of the way
    if payload.get("hook_event_name") == "SessionStart":
        out = run_session_start(payload)
    else:
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

    if assessment.get("graph_context") is False:
        lines.append(
            "Note: no dependency graph yet (building in background) — "
            "structural blast-radius signal unavailable for this verdict."
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
