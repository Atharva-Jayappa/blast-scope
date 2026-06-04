# Scoring heuristics

How blast-scope turns a shell command into a 0.0–1.0 risk score. Documented as
it evolves; every constant here is calibrated against
`tests/fixtures/eval_corpus.jsonl` (see [calibration](#calibration)).

## The two-axis model

Risk is **blast radius** divided by **recoverability** — orthogonal questions:

```
                 ┌─────────────── blast radius ───────────────┐
   score  =  command_weight  ×  structural  ×  (1 / reversibility_factor)
                                   │                    │
              how much depends on it (graph)     can it be undone?
```

- **command_weight** — from `command_effects.py`. Flag/operand-sensitive:
  `rm -rf` ≠ `rm`, `git reset --hard` ≠ `git status`, redirect-clobber `>` ≠
  append `>>`. Reads weigh ~0.
- **structural** — `max(normalized_in_degree, pagerank_importance)`. A file is
  high blast radius if *either* many things import it directly *or* it is
  globally central (weighted PageRank over the dependency graph). OR-semantics,
  not a tuned weighted sum.
- **reversibility_factor** — `max(0.25, 2.0 − 1.75 × irrecoverability)`. Fully
  recoverable (git-clean) ⇒ 2.0 (halves risk); gone-for-good ⇒ 0.25 (≈×4).
  `× 0.5` again if the command is recursive.

## Recoverability categories

From `recoverability.py` (`classify_path`). After the raw score, a category
**floor or cap** is applied — the multiplicative axis alone can't say
"irreplaceable regardless of importers" or "always cheap to rebuild":

| category        | irrecoverability | effect on score | rationale |
|-----------------|------------------|-----------------|-----------|
| `absent`        | 0.0              | cap ≤ 0.10      | nothing to lose |
| `regenerable`   | 0.05             | cap ≤ 0.15      | node_modules/dist/__pycache__ rebuild |
| `tracked_clean` | 0.2              | —               | recoverable from git |
| `tracked_dirty` | 0.55             | —               | uncommitted changes would be lost |
| `untracked`     | 0.7              | floor ≥ 0.20    | not in history ⇒ unrecoverable |
| `precious_data` | 0.85             | floor ≥ 0.60    | *.tfstate / *.db / *.dump |
| `gitignored`    | 0.85             | floor ≥ 0.60    | not in history |
| `secret`        | 0.9              | floor ≥ 0.85    | .env / *.pem / id_rsa — sensitive + unrecoverable |

Caps/floors apply only to state-changing commands (not pure reads).

## Out-of-graph consequences

The import graph is blind to whole classes of blast radius. `consequences.py`
gathers these; the strongest floor per domain is applied. **Where** in the
pipeline matters:

- **path-tied** (`infra`, `config`) — floor applied **before** the
  recoverability caps, so a regenerable/absent target still caps low
  (`rm node_modules` stays safe even if code references it).
- **VCS** (`vcs`) — applied **after** the caps and **ungated**, because the
  danger is in the working-tree/history state, *not* the operand. git's
  "targets" are subcommands/refs, not files — so the target's recoverability
  (e.g. the bogus `absent` from treating `reset` as a path) must not suppress a
  real working-tree consequence. `vcs.analyze_git` only fires for genuinely
  destructive ops, so it is self-gating.

Domain floors:

- **VCS** — *context-aware*. `git reset --hard` on a clean tree is 0.0; floor
  scales with the working-tree state it would destroy
  (`min(0.9, 0.4 + 0.05 × n)` for n modified/untracked files). `push --force`
  0.7, `rebase`/`filter-branch` 0.6, `branch -D` 0.4.
- **infra** — Dockerfile, compose, `*.tf`/`*.tfvars`, k8s/helm, CI workflows ⇒
  floor 0.6. Real deploy-time impact, zero AST in-degree.
- **config/data** — files loaded by *path string* (`open("config.yaml")`) that
  no import edge points at. Bounded source scan; floor
  `min(0.85, 0.45 + 0.05 × references)`.

## Command classes & the eligibility filter

Beyond the filesystem, blast-scope scores four more command classes — **git,
docker, pip/uv, SQL** — through a uniform two-stage protocol in
`src/blast_scope/classes/`. Each runs cheaply first, probes only when warranted:

1. **triage** — a near-free check ("is this my class, and destructive?"). Pure
   string/flag inspection, no subprocess. The common command matches nothing and
   exits here.
2. **assess** — only for triaged candidates. A strictly **read-only probe** when
   available, else a **labeled heuristic estimate** (`estimated=True`). Returns a
   `Consequence` floor, exactly like the analyzers above.

A class earns a *live probe* only when both gates hold — this is the design
boundary, not an implementation detail:

- **Safe probe exists** — impact is observable by a side-effect-free read
  (HTTP-GET sense: zero observable side effects). Never mutate state to assess
  state. State-mutating "probes" (`terraform plan`, `kubectl --dry-run=server`)
  are out of scope by construction.
- **Authorable reversibility** — the undo story is well-known enough to encode
  in a static per-class table, refined by the probe.

Radius × reversibility is combined **per class** (there is no single global
formula) — the floor each class emits already encodes its own treatment:

| Class | Probe (read-only) | Floor logic (abridged) |
|---|---|---|
| **git** (`vcs.py` + `classes/git.py`) | `status` · `reflog` · `rev-list` · `rev-parse @{u}` | working-tree-scaled base; `push --force` escalates on a diverged/protected upstream; `branch -D` drops to ~0.25 when fully merged |
| **docker** (`classes/docker.py`) | `volume inspect` · `ps -a` · `volume ls` | volume rm → 0.85–0.9 (data, no image to rebuild); container rm -f → ~0.4 (recreatable); `system prune --volumes` scales with unused-volume count |
| **pip/uv** (`classes/packages.py`) | read lockfile/manifest (no subprocess) | lockfile present → 0.15 (regenerable); absent → 0.35 |
| **SQL** (`classes/sql.py`) | SQLite `SELECT count(*)` `mode=ro`; transaction check | DROP 0.9 / TRUNCATE 0.85 / DELETE-no-WHERE 0.75; open transaction → ~0.6 (ROLLBACK-able); Postgres/MySQL → estimate |

**Floor placement.** These are *state-tied* domains (the danger is in runtime
state, not a filesystem path), so — like `vcs` — their floors apply **after** the
recoverability caps and ungated (`_STATE_TIED_DOMAINS` in `risk_scorer.py`).
Path-tied `infra`/`config` still apply before the caps.

**Graceful degradation.** Probe → (missing daemon / no driver / timeout / error)
labeled estimate → (triage error) silent. Analysis is advisory and must never
block or delay a command on failure.

## Severity & recommendation

| score      | severity  | recommendation |
|------------|-----------|----------------|
| ≥ 0.80     | critical  | block          |
| ≥ 0.50     | high      | confirm        |
| ≥ 0.20     | medium    | confirm        |
| < 0.20     | low       | proceed        |

("block"/"confirm" are advice — the hook never hard-blocks.)

## Calibration

`eval.py` runs the labeled corpus (`tests/fixtures/eval_corpus.jsonl`, 33 cases:
14 low / 4 medium / 9 high / 5 critical, spanning every recoverability category,
git clean-vs-dirty state, infra/config files, a graph-indexed central module, and
the git/docker/pip/SQL classes — including degrade-to-estimate paths). Each case
is materialized in a throwaway project — including git working-tree state and,
when needed, a built dependency graph — then scored with the real `assess()`. It
reports:

- **exact severity** and **within-one-band** accuracy
- **gate** precision/recall/F1 (proceed vs. flag, truth = not-low)
- a confusion matrix and a list of mismatches

Run it: `python -m blast_scope.eval`.

**Current calibration:** 33/33 exact, 33/33 within-one-band, gate F1 1.00.
`tests/test_eval.py` pins these with headroom (exact ≥ 0.85, within ≥ 0.95,
F1 ≥ 0.9, and no critical-labeled command ever scored `proceed`) so future
tuning can't silently regress.

Three calibration fixes came out of the first runs, all in `risk_scorer.py`:

1. Consequence floors were gated on `command_weight > 0`, which is 0 for
   `git checkout`/`push` — so VCS consequences never applied. Re-gated path-tied
   floors on `intent != "read"`.
2. `untracked` files (unrecoverable) had no floor and scored `low`; added a 0.20
   floor so losing an unrecoverable file is at least `medium`.
3. The `absent` cap was crushing VCS floors to 0.10 (git's subcommand token gets
   mis-classified as an absent path). Split consequence handling so VCS floors
   apply *after* the caps and ungated; path-tied floors stay before the caps.

When adding a heuristic, add corpus cases that exercise it and re-run the harness
before trusting the number.
