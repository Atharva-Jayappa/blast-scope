# Blast Scope

**A consequence engine for shell commands.** Blast Scope scores what a command
would actually *do* — before an AI agent (or you) runs it. It doesn't pattern-match
on syntax; it resolves targets against a dependency graph, the git working tree,
secret/data classification, and infrastructure context, then returns a structured
risk score with evidence.

The whole point is contextual blast radius. The *same command* gets a completely
different score depending on what it would hit:

```
rm -rf ./logs        →  LOW       0 importers, regenerable, outside src tree.        proceed
rm -rf ./config      →  CRITICAL  8 modules import it, high PageRank centrality.     block
rm .env              →  CRITICAL  secret, unrecoverable — even with 0 importers.     block
git reset --hard     →  LOW       working tree is clean — nothing to discard.        proceed
git reset --hard     →  HIGH      would discard 4 files with uncommitted changes.    confirm
```

> Not a blocklist. Not a replacement for Shellfirm. Not a syscall monitor. It
> scores *structural consequence* — and for risky commands it captures an undo
> snapshot first.

---

## How it works

```
shell command
     │
     ▼
┌─────────────┐   split chains (&& || ; |), de-alias PowerShell cmdlets,
│   parse     │   classify intent + flags (rm -rf ≠ rm, > ≠ >>)
└─────┬───────┘
      │ targets, flags, intent
      ▼
┌──────────────────────────── score (two axes) ────────────────────────────┐
│                                                                           │
│   blast radius                              recoverability                │
│   ────────────                              ──────────────                │
│   command_weight                            git tracked / dirty           │
│   × structural   ←── dependency graph       regenerable (node_modules)    │
│      (in-degree, PageRank centrality)       secret (.env/.pem/id_rsa)     │
│                                             precious (*.tfstate/*.db)     │
│                                                                           │
│              ÷  reversibility_factor  ──────────────┘                     │
│                                                                           │
│   + out-of-graph consequences (floors):                                   │
│       VCS   — git reset/clean/push --force, scaled by working-tree state  │
│       infra — Dockerfile, Terraform, k8s, CI configs                      │
│       config— files loaded by path string the import graph can't see      │
└─────────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
        score 0.0–1.0 → severity (low/medium/high/critical) → recommendation
                                   │
                                   ▼
              PreToolUse hook: advise + snapshot risky targets (undoable)
```

See [docs/heuristics.md](docs/heuristics.md) for the exact formula, weight tables,
and calibration.

---

## Status

**v0.1.0 — working end-to-end, not yet on PyPI.** Built in four phases:

| Capability | Module |
|---|---|
| Flag/operand-sensitive command model (POSIX **and** PowerShell) | `command_effects.py`, `command_parser.py` |
| Recoverability classification (git state, secrets, regenerable, precious data) | `recoverability.py` |
| Dependency graph + weighted **PageRank** centrality, incremental indexing | `graph_resolver.py`, `centrality.py` |
| Two-axis, evidence-based scoring | `risk_scorer.py` |
| Out-of-graph **consequence analyzers** (VCS / infra / config-by-path) | `consequences.py`, `vcs.py`, `infra.py`, `config_refs.py` |
| **PreToolUse hook** + tarball **snapshot/undo** | `hook.py`, `snapshot.py` |
| **Eval harness** + labeled corpus + calibration | `eval.py`, `tests/fixtures/eval_corpus.jsonl` |

**Calibration (2026-05-30):** on a 28-case labeled corpus spanning every
recoverability category, git state, infra/config files, and a graph-indexed
case — **28/28 exact severity, gate F1 1.00.** `tests/test_eval.py` pins these
with headroom so future changes can't silently regress. Run it yourself:

```bash
uv run python -m blast_scope.eval
```

---

## Installation

```bash
git clone https://github.com/Atharva-Jayappa/blast-scope.git
cd blast-scope
uv sync --all-extras
```

Or directly:

```bash
uv pip install git+https://github.com/Atharva-Jayappa/blast-scope.git
```

---

## Usage

### As an MCP server

Add to your MCP client config (e.g. Claude Code `settings.json`):

```json
{
  "mcpServers": {
    "blast-scope": { "command": "blast-scope", "type": "stdio" }
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

### As a PreToolUse hook (advise + auto-snapshot)

Intercept Bash commands *before* they run — advisory, never blocking, and it
snapshots destructive targets so any mistake is reversible (even files git
can't recover). Add to `.claude/settings.json`:

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
│   ├── consequences.py      # coordinator for out-of-graph analyzers
│   ├── vcs.py / infra.py / config_refs.py   # consequence analyzers
│   ├── hook.py              # PreToolUse advisory hook
│   ├── snapshot.py          # tarball snapshot / restore / list
│   ├── eval.py              # evaluation harness + metrics
│   └── vendor/crg/          # vendored from code-review-graph (MIT)
├── tests/                   # 230+ tests incl. eval regression guard
│   └── fixtures/eval_corpus.jsonl   # labeled calibration corpus
└── docs/
    ├── heuristics.md        # scoring model + calibration
    └── hook.md              # hook registration + undo
```

---

## Roadmap

- Calibrate against a larger, real-world corpus (current corpus is 28 cases).
- PowerShell-shell awareness in the hook path (the MCP tool already supports it).
- Optional richer interception modes beyond advisory.

See [CLAUDE.md](CLAUDE.md) for the full spec, contracts, and design rules.
