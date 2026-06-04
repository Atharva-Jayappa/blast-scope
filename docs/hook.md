# PreToolUse hook + snapshot/undo

blast-scope can intercept shell commands *before they run* via a Claude Code
`PreToolUse` hook. The hook is **advisory** — it never blocks — and its volume
is **tiered by severity** so the rare loud message keeps its signal:

| Severity | What the hook does |
|---|---|
| low / medium | **silent** — the common case never costs the agent attention |
| high | **advise** — returns the blast-radius assessment as `additionalContext` |
| critical | **advise + snapshot** — also captures an undo snapshot of the destructive targets |

Surfacing every command at one volume is what trains an agent to ignore *all*
of it, including the critical one. Keeping low/medium quiet is what lets the
critical advisory land.

If a critical command turns out to be a mistake, its snapshot can be restored —
even for files git can't recover (untracked, `.env`, `*.tfstate`, git-ignored
dirs).

The severity it tiers on comes from the full multi-class engine: filesystem
(dependency graph + git), plus **git / docker / pip·uv / SQL** command classes,
each scored from a strictly read-only probe (or a labeled estimate when no probe
is available — e.g. `docker volume rm` with no daemon, or `DROP TABLE` against a
remote Postgres). So `docker volume rm pgdata` or `psql -c "DROP TABLE users"`
reaches the advisory the same way `rm -rf ./config` does. See
[heuristics.md](heuristics.md#command-classes--the-eligibility-filter).

### Snapshot policy

The snapshot is the actual safety net, so it stays fast and trustworthy by
consulting what `recoverability.py` already knows instead of taring blindly:

- **Recoverable targets are skipped.** Git-clean-tracked files (git has them)
  and regenerable dirs like `node_modules` / `dist` (cheap to rebuild) are not
  archived — snapshotting them is pure redundancy.
- **Oversize targets are warned, not tarred.** A target past a hard size cap
  (256 MiB) is reported in the advisory rather than archived, so a multi-GB
  tree can never stall the agent or fill the disk mid-run.

A mixed command like `rm .env node_modules` therefore snapshots only `.env`.

## How it works

```
Bash command ──▶ PreToolUse hook ──▶ assess() ──▶ score
                                          │
                       severity < high    ├─▶ silent (return nothing)
                                          │
                       severity = high    ├─▶ additionalContext (risk only)
                                          │
                       severity = critical├─▶ snapshot recoverable-filtered,
                                          │   size-capped targets
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
