# PreToolUse hook + snapshot/undo

blast-scope can intercept shell commands *before they run* via a Claude Code
`PreToolUse` hook. The hook is **advisory** — it never blocks. For anything
medium-risk or worse it does two things:

1. Returns the blast-radius assessment as `additionalContext`, so the agent
   sees the consequence before it proceeds.
2. Captures an **undo snapshot** of the paths the command would destroy.

If the command turns out to be a mistake, the snapshot can be restored — even
for files git can't recover (untracked, `.env`, `*.tfstate`, git-ignored dirs).

## How it works

```
Bash command ──▶ PreToolUse hook ──▶ assess() ──▶ score
                                          │
                        severity >= medium├─▶ snapshot destructive targets
                                          │
                                          └─▶ additionalContext (risk + snapshot id)
```

The hook entry point is `python -m blast_scope.hook`. It reads the PreToolUse
JSON on stdin and writes hook output on stdout. To keep per-command latency
low it uses the dependency graph **only if one is already built**
(`<root>/.blast-scope/graph.db`); it never triggers a graph build. Run
`index_project` (or the MCP tool) once to enable graph-aware scoring.

## Registration

Add to `.claude/settings.json` (project) or `~/.claude/settings.json` (global):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python -m blast_scope.hook" }
        ]
      }
    ]
  }
}
```

Optionally set `BLAST_SCOPE_PROJECT_ROOT` so the hook scores against and stores
snapshots under a fixed project root rather than the command's `cwd`.

## Undo

Snapshots live in `<root>/.blast-scope/snapshots/<id>/` (git-ignored). List and
restore them through the MCP tools:

- `list_snapshots(project_root)` → newest-first snapshots with their captured paths.
- `restore_snapshot(snapshot_id, project_root)` → restores those paths in place,
  overwriting whatever is there now.

Or programmatically:

```python
from blast_scope import snapshot
snaps = snapshot.list_snapshots("/proj")
snapshot.restore_snapshot(snaps[0]["id"], root="/proj")
```

## Why advisory, not blocking

blast-scope scores consequence; it does not maintain a blocklist. A hard block
on a pattern is exactly the brittle behavior this project exists to replace. The
hook surfaces *why* a command is risky and makes the dangerous case reversible —
the agent (or human) stays in control of the decision.
```
