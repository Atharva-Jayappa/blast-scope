# Blast Scope

<!-- mcp-name: io.github.atharva-jayappa/blast-scope -->

**A consequence engine for shell commands.** Blast Scope scores what a command
would actually *do* — before an AI agent (or you) runs it. It doesn't pattern-match
syntax into a blocklist; it figures out the command's **real target**, observes
that target with a **safe, read-only probe**, and returns a structured risk score
with evidence.

The whole point is *contextual* blast radius. The **same command** gets a
completely different score depending on what it would actually hit:

```
COMMAND                            SEVERITY   WHY                                          ADVICE
─────────────────────────────────  ────────   ──────────────────────────────────────────  ───────
rm -rf ./logs                      LOW        0 importers · regenerable · outside src      proceed
rm -rf ./config                    CRITICAL   8 modules import it · high PageRank hub      block
git reset --hard   (clean tree)    LOW        nothing uncommitted to discard               proceed
git reset --hard   (4 dirty files) HIGH       would throw away 4 files of uncommitted work confirm
git push --force   (protected)     CRITICAL   would orphan commits on a protected branch   block
docker volume rm cache  (absent)   LOW        volume doesn't exist — nothing to remove     proceed
docker volume rm pgdata (in use)   CRITICAL   holds data · in use · no image to rebuild    block
pip uninstall flask     (uv.lock)  LOW        regenerable — exact version pinned in lock   proceed
DROP TABLE users        (42 rows)  CRITICAL   schema + 42 rows · irreversible              block
DELETE FROM logs        (in txn)   HIGH       no WHERE — but inside a txn, ROLLBACK-able    confirm
```

Two commands can be byte-identical and score four bands apart. **That gap is the
product.**

> Not a blocklist. Not a replacement for Shellfirm. Not a syscall monitor. It
> scores *structural consequence* — advisory, never blocking — and for the rare
> critical command it captures an undo snapshot first.

---

## How it works

A command flows through a cheap funnel: almost everything is recognized as
non-destructive in microseconds and exits silent. Only a flagged *destructive
candidate* pays for a probe.

```
  shell command
      │   split chains (&& || ; |) · de-alias PowerShell · parse flags/targets
      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  STAGE 1 · triage  (near-free regex — runs on every command)          │
  │     which class?   git · docker · pip/uv · sql · else filesystem       │
  │     destructive?   `git status` → no.   `git reset --hard` → yes ↓     │
  └───────────────────────────────┬──────────────────────────────────────┘
                    destructive candidate │   (everything else exits here, silent)
                                          ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  ELIGIBILITY FILTER   safe read-only probe?   AND   undo authorable?   │
  └──────────────┬──────────────────────────────────────┬─────────────────┘
         yes, probe it │                       no probe here / now │
                       ▼                                           ▼
   STAGE 2 · safe probe (read-only)                    heuristic estimate
     git  status · reflog · rev-list                   from a static per-class
     docker  inspect · ps · ls                          table — and LABELED
     sqlite  SELECT count(*)  [mode=ro]                  "(estimated)" so you
     pip/uv  read lockfiles                              know it wasn't probed
                       │                                           │
                       └─────────────────────┬─────────────────────┘
                                              ▼
        blast radius  ×  reversibility   (combined PER CLASS — no global formula)
        filesystem also folds in: dependency-graph centrality + recoverability
                                              ▼
              score 0.0–1.0  →  severity (low / medium / high / critical)
                                              ▼
        PreToolUse hook:  silent (low/med) · advise (high) · advise + snapshot (critical)
```

**The eligibility filter is the design boundary.** A command class earns a *live
probe* only when both hold: (1) its impact is observable by a **strictly
side-effect-free read** (HTTP-GET sense — never mutate state to assess state),
and (2) its undo story is well-known enough to encode in a static table. When a
probe can't run here and now (no docker daemon, no DB driver, no creds), the tool
degrades to a labeled estimate — it never guesses silently, and it never blocks.

See [docs/heuristics.md](docs/heuristics.md) for the per-class tables, the exact
filesystem formula, and calibration.

### The five command classes

| Class | Destructive ops it scores | Safe (read-only) probe | Reversibility signal |
|---|---|---|---|
| **Filesystem** | `rm -rf`, `mv`, `>` truncate | dependency graph + git status | git-tracked? regenerable? secret? precious? |
| **Git** | `reset --hard`, `push --force`, `branch -D`, `clean -fdx` | `status` · `reflog` · `rev-list` · `rev-parse @{u}` | reflog window · remote ahead · protected branch |
| **Docker** | `volume rm`, `system prune -a`, `rm -f` | `volume inspect` · `ps -a` · `volume ls` | volume → none · container → recreatable from image |
| **pip / uv** | `pip uninstall`, `uv pip uninstall` | read lockfile / manifest (no subprocess) | lockfile present → fully regenerable |
| **SQL** | `DROP`, `TRUNCATE`, `DELETE` without `WHERE` | SQLite: `SELECT count(*)` `mode=ro`; transaction check | inside a transaction? backup posture? |

New classes drop in behind one protocol (`triage` / `probe_commands` / `assess`)
in [`src/blast_scope/classes/`](src/blast_scope/classes); the probe surface each
declares is asserted read-only by the test suite, so no probe can ever mutate.

---

## Status

**v0.3.0 — calibrated multi-class guardrail with a precise dependency graph.**

| Capability | Module |
|---|---|
| Flag/operand-sensitive command model (POSIX **and** PowerShell) | `command_effects.py`, `command_parser.py` |
| Recoverability classification (git state, secrets, regenerable, precious data) | `recoverability.py` |
| Dependency graph + weighted **PageRank** centrality, incremental indexing | `graph_resolver.py`, `centrality.py` |
| Two-axis, evidence-based filesystem scoring | `risk_scorer.py` |
| **Command-class probes** — git / docker / pip·uv / SQL, behind one protocol | `classes/` |
| Out-of-graph **path analyzers** (infra / config-by-path) + git base | `consequences.py`, `vcs.py`, `infra.py`, `config_refs.py` |
| **PreToolUse hook** + tarball **snapshot/undo** | `hook.py`, `snapshot.py` |
| **Eval harness** + labeled corpus + calibration | `eval.py`, `tests/fixtures/eval_corpus.jsonl` |

**Calibration.** Two harnesses, both run-it-yourself:

- **In-repo corpus** (`tests/fixtures/eval_corpus.jsonl`, 38 cases spanning every
  recoverability category, git working-tree state, infra/config, `rm -rf .git`,
  a graph-indexed central module, and the git/docker/pip/SQL classes) —
  **38/38 exact severity, gate F1 1.00**, pinned by `tests/test_eval.py` with
  headroom so changes can't silently regress.
- **[SABER](https://github.com/sssr-lab/saber)** — 716 real coding-agent
  workspaces. Against ~1725 safe commands, blast-scope's **false-positive rate is
  0.4%**; on its core competency (`data_destruction`) it catches **82%** of
  injected attacks, on realistic workspaces. The honest per-category recall (low
  on out-of-scope exfiltration/persistence — a different threat model) is the
  probe roadmap. See [`bench/`](bench).

```bash
uv run python -m blast_scope.eval                 # in-repo corpus
python bench/saber_eval.py --tasks <saber>/dataset/data/tasks.jsonl   # SABER
```

---

## Installation

The fastest path for any MCP client is zero-install via `uvx` (no clone, no venv):

```bash
uvx blast-scope        # runs the MCP server on stdio
```

**Claude Code users — one line wires up both the MCP tools and the advisory hook:**

```bash
/plugin marketplace add Atharva-Jayappa/blast-scope
/plugin install blast-scope
```

For development, or to pin a checkout:

```bash
git clone https://github.com/Atharva-Jayappa/blast-scope.git
cd blast-scope && uv sync --all-extras
```

---

## Usage

### As an MCP server

Add to your MCP client config (e.g. Claude Code `settings.json`):

```json
{
  "mcpServers": {
    "blast-scope": { "command": "uvx", "args": ["blast-scope"], "type": "stdio" }
  }
}
```

Tools exposed:

| Tool | Purpose |
|---|---|
| `assess_command(command, cwd?, project_root?)` | Score a (possibly chained) command. Returns score, severity, rationale, evidence, recoverability, affected nodes, and a per-segment `chain` breakdown. |
| `index_project(project_root)` | Force a dependency-graph rebuild (auto-built on first use otherwise). |
| `list_snapshots(project_root)` | List undo snapshots, newest first. |
| `restore_snapshot(snapshot_id, project_root)` | Undo a risky command by restoring its snapshot. |

### As a PreToolUse hook (tiered advice + auto-snapshot)

Intercept Bash commands *before* they run — advisory, never blocking. Volume
scales with stakes: **silent** on low/medium, **advise** on high, **advise +
snapshot** on critical. The snapshot skips what's already recoverable
(git-clean, regenerable) and warns rather than tars anything over a hard size
cap, so the undo net stays fast and trustworthy. Add to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python -m blast_scope.hook" }] }
    ]
  }
}
```

Full details and the undo flow: [docs/hook.md](docs/hook.md).

---

## Example output

A filesystem command, scored against the dependency graph:

```jsonc
// assess_command("rm -rf ./config", project_root="/proj")
{
  "score": 0.93,
  "severity": "critical",
  "recommendation": "block",
  "recoverability": "untracked",
  "rationale": "rm targets config. 8 direct importer(s), 14 total affected. not git-tracked. recursive deletion. CRITICAL risk.",
  "evidence": [
    "8 importer(s), 14 affected node(s)",
    "high centrality (PageRank 0.91) — a hub other code routes through",
    "untracked — not in git history",
    "recursive — applies to every file underneath"
  ],
  "affected_nodes": [ /* ... */ ],
  "chain": [ /* per-segment breakdown */ ]
}
```

A command class that couldn't probe — note the **labeled estimate** (no
Postgres driver, server possibly remote, so the tool refuses to guess silently):

```jsonc
// assess_command('psql -c "DROP TABLE users"')
{
  "score": 0.9,
  "severity": "critical",
  "recommendation": "block",
  "evidence": [
    "drops users — its schema and all rows, irreversible (estimated — no read-only probe for postgres)"
  ]
}
// the same DROP against a local SQLite file probes for real:
//   "drops users — its schema and 42 row(s), irreversible"   (estimated: false)
```

---

## Development

```bash
uv sync --all-extras
uv run pytest -q              # full suite
uv run python -m blast_scope.eval   # scoring accuracy report
```

### Project structure

```
blast-scope/
├── src/blast_scope/
│   ├── server.py            # MCP server + tools (assess, index, snapshots)
│   ├── command_parser.py    # shell → structured intent (POSIX + PowerShell)
│   ├── command_effects.py   # command/flag/operand → intent + weight
│   ├── recoverability.py    # path → how recoverable if destroyed
│   ├── graph_resolver.py    # paths → dependency-graph impact (+ PageRank)
│   ├── centrality.py        # pure-Python weighted PageRank
│   ├── risk_scorer.py       # signals → score + severity + evidence
│   ├── classes/             # command-class probes behind one protocol
│   │   ├── __init__.py      #   Candidate · ConsequenceClass · registry
│   │   ├── git.py           #   reflog / upstream-divergence / protected branch
│   │   ├── docker.py        #   volume / container / system-prune probes
│   │   ├── packages.py      #   pip·uv uninstall vs. lockfile presence
│   │   └── sql.py           #   DROP/TRUNCATE/DELETE — SQLite probe + estimates
│   ├── consequences.py      # coordinator: class probes + path analyzers
│   ├── vcs.py / infra.py / config_refs.py   # git base + path analyzers
│   ├── hook.py              # PreToolUse advisory hook
│   ├── snapshot.py          # tarball snapshot / restore / list
│   ├── eval.py              # evaluation harness + metrics
│   └── vendor/crg/          # vendored from code-review-graph (MIT)
├── tests/                   # 295+ tests incl. eval regression guard
│   └── fixtures/eval_corpus.jsonl   # labeled calibration corpus
└── docs/
    ├── heuristics.md        # scoring model + per-class tables + calibration
    └── hook.md              # hook registration + undo
```

---

## Roadmap

- Calibrate against a larger, real-world corpus (current corpus is 33 cases).
- Optional live probes for Postgres/MySQL (in-process, read-only) once a driver
  policy is settled — today those engines degrade to labeled estimates.
- PowerShell-shell awareness in the hook path (the MCP tool already supports it).
- Optional richer interception modes beyond advisory.

See [CLAUDE.md](CLAUDE.md) for the full spec, contracts, and design rules.
