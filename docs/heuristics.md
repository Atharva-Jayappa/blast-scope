# Scoring heuristics

How blast-scope turns a shell command into a 0.0–1.0 risk score. Documented as
it evolves; every constant here is calibrated against
`tests/fixtures/eval_corpus.jsonl` (see [calibration](#calibration)).

## Resolution — score the command the kernel sees, not the string

The shell is a two-stage machine: stage one rewrites the text (brace → tilde →
parameter → word-split → glob expansion), stage two executes the result.
`rm -rf $BUILD_DIR/` with `BUILD_DIR` unset *executes* as `rm -rf /`. Before
any scoring, `resolution.py` runs stage one statically — pure text
transformation plus read-only filesystem lookups, never executing the analyzed
command — so every axis below scores the resolved command:

- **env/tilde/brace** — expanded against an explicit env mapping (the hook's
  process env; the eval corpus declares per-case env). Quoting is honored:
  single quotes suppress everything, double quotes expand `$` but suppress
  glob/word-split. `${VAR:-x}`-style operators stay literal, noted.
- **glob** — bash semantics: matches replace the pattern (capped inline at 20,
  scan at 1000); **no match keeps the pattern literal** (nullglob off), noted.
  Expanded file lists flow into `targets`, so the graph/recoverability/
  consequence axes all see real files.
- **word splitting** — only *expanded* text splits (`FILES="a b"; rm $FILES`
  is two targets), matching bash.
- **symlinks** — traversals surface in evidence ("'./cache' is a symlink →
  /var/lib/app/data"); the destination is what gets classified.

**Unset-variable hazards** (domain `resolution`, state-tied, destructive-gated):
an unset/empty var whose removal collapses the path to `/` or `$HOME` floors at
**0.85**; one that silently re-roots the path (`$APP_DIR/cache` → `/cache`)
floors at **0.6**. The residue often classifies `absent` — which is exactly why
these floors apply after the caps, like VCS.

### Script transparency

`npm run clean` contains nothing to score; the danger lives in package.json.
`expand_indirection` rewrites wrappers into what they execute, then the normal
pipeline scores that:

- `sh|bash|zsh -c '...'` → the payload.
- `npm|pnpm|yarn|bun run X` → `preX` + `X` + `postX` from package.json (npm
  runs all three — a destructive pre-hook can't hide behind an innocent name).
- `bash foo.sh` / `./foo.sh` / `source foo.sh` → the script's statements
  (depth ≤ 2, ≤ 200 statements, size-capped).
- `make target` → the target's recipe plus direct prerequisites, parsed
  **statically**. Never `make -n`: GNU make executes `$(shell ...)` during
  Makefile *parsing*, dry-run or not.

Wrappers that stay opaque (`python -c`, `node -e`, unreadable scripts,
`$(shell)` recipes) emit an `opaque_wrapper` floor of **0.35**; `curl | sh`
emits `pipe_to_shell` at **0.5**. Principle: *not seeing inside must never
score lower than seeing inside and finding it harmless.*

### Read-only command substitution

`rm -rf $(find . -name '*.log')` names its targets through an inner command's
output. When that inner command is **provably a target-list producer** —
deny-by-default allowlist checking verb *and* flags (`find` without
`-delete/-exec`, `ls`, `git ls-files/rev-parse/describe`, `basename`, `echo`,
…), no metacharacters or control chars, no nesting, and **every path argument
inside the working tree** — the resolver runs *just it* (2 s timeout) and
substitutes its output. Anything else stays unexecuted; on a destructive verb
the invisible target list becomes an `unresolved_substitution` floor of
**0.35**.

Deliberately **not** on the allowlist: any command that reads file *content*
(`cat`/`head`/`tail`/`wc`) or reveals paths outside the tree (`realpath`,
`ls /etc`, `find /`). "Read-only" is not the same as "safe to run during
analysis" — reading a secret *is* the harm. Allowing `cat` once made
`rm -rf $(cat ~/.aws/credentials)` read the file and surface its contents
during scoring; the allowlist expands target lists, never discloses bytes.

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
  not a tuned weighted sum. Both signals come from a **precise file-level import
  graph** resolved with stdlib `ast` (module→file the way Python does it,
  relative imports included) — so "8 modules import `config.py`" is a real count,
  not a name-match. Projects with no resolvable Python imports fall back to the
  tree-sitter graph. Node-level detail (`affected_nodes`) stays from tree-sitter.
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
| `repo_history`  | 0.9              | floor ≥ 0.70    | deleting `.git`/the repo root removes the recovery net itself |
| `system_root`   | 0.95             | floor ≥ 0.90    | the filesystem root or `$HOME` — nothing above it in blast radius |
| `precious_data` | 0.85             | floor ≥ 0.60    | *.tfstate / *.db / *.dump |
| `gitignored`    | 0.85             | floor ≥ 0.60    | not in history |
| `secret`        | 0.9              | floor ≥ 0.85    | .env / *.pem / id_rsa — sensitive + unrecoverable |

**Caps vs. floors gate differently.** *Caps* (`absent`/`regenerable`, which only ever
*lower* the score) apply to any state-changing command. *Floors* (the "losing
this is catastrophic" rows) apply only when the command's intent is
**destructive** — not merely non-read. A command that *names* a sensitive file
but only reads or copies it (`sqlite3 app.db '.tables'`, `hexdump key.pem`,
`cp config.toml dest`) must not inherit a deletion's blast radius; gating these
floors on `weight > 0` alone was the dominant false-positive source on the SABER
corpus (see [calibration](#calibration)).

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

## Dry-run oracles — observe, don't predict

For a class of destructive commands the tool itself can be asked, side-effect-
free, exactly what would be destroyed. Oracles replace count-based estimates
with **exact target lists**, which flow through one channel
(`Consequence.targets`) into three consumers at once: worst-case
recoverability re-classifies the real victims, the mass-destruction gate
counts real source files, and the **hook's undo snapshot archives them** —
files whose paths appear nowhere in the command text.

| Command | Oracle (verified side-effect-free) | Output |
|---|---|---|
| `git clean -f[dxX]` | `git clean -n` + mirrored selection flags (`-d`/`-x`/`-X`/`-e`/pathspec — dropping one understates) | exact `Would remove` list; nested-repo skips flagged (real `-ff` removes more) |
| `git reset --hard <ref>` | `git rev-list --count <ref>..HEAD` | orphaned-commit count; floor 0.45 (reflog keeps them ~30–90 d) or 0.6 with no reflog; dirty-tree scaling unchanged — uncommitted work is the real loss |
| `git checkout/restore` paths | `git diff --name-only HEAD [-- paths]` | exact dirty files clobbered (committed diffs just switch — not loss) |
| `find … -delete` / `-exec rm` | faithful `-print` rewrite: terminal replaced **in place** + `-depth` added (`-delete` implies it; `-prune` diverges without it); `-o` / multi-terminal → punt to static | exact match set; `rm -r` payloads flagged as subtree roots |
| `sqlite3 db "DELETE … WHERE p"` | `SELECT count(*) FROM t WHERE p` on the `mode=ro` connection (single-table only; `LIMIT n` capped) | matched/total: <10 % stays low, ≥50 % or ≥1000 rows floors 0.6 — a "scoped" delete removing most of the table is a mass delete in a wig |
| `rsync --delete` (local↔local) | real flags + `--dry-run --itemize-changes` (man page guarantees parity with the real run); remote endpoints → estimate, never probed | `*deleting` lines |
| `cp`/`mv` over existing file | pure `stat` of the destination | overwrite made explicit in evidence |

**Verified UNSAFE as probes — never used:** `make -n` (`$(shell)` runs at
parse), `npm --dry-run` (lifecycle scripts + network), `EXPLAIN ANALYZE` on a
DELETE (executes it), `BEGIN…ROLLBACK` (transient mutation), `chmod
--changes`/`cp -n`/`mv -n` (execution-time reporters, not previews), any
user-defined git alias (may expand to `!sh -c …`).

Degradation contract unchanged: probe unavailable (no rsync on stock Windows,
Windows `find.exe` being a string-search tool, missing ref) → labeled
estimate or silent fallback to the static classification. A failing probe
never blocks and never scores lower than not probing.

## Speculative execution (opt-in — the ground-truth oracle)

The dry-run oracles rewrite or preview a command without running it. Speculative
execution (`speculate.py`) goes one step further on the ladder: it **runs the
command** against a disposable copy-on-write copy of the working tree and diffs
the scratch layer to observe *exactly* what was created, modified, or deleted —
no rewriting, no per-verb cleverness, the kernel does the work. The observed
deletions/overwrites feed the same `Consequence.targets` channel the static
oracles use, so recoverability, the mass gate, and the undo snapshot all see
the real victims.

This is the one analyzer that executes the analyzed command, so isolation is
layered and any single layer suffices:

1. **overlayfs never writes the lower layer.** The real tree is the read-only
   lowerdir; every write lands in a throwaway upperdir. A kernel guarantee.
2. **Private mount namespace** (`unshare --mount`) — the overlay can't leak
   into the parent mount table.
3. **Severed network namespace** (`unshare --net`) — no interfaces, so a
   "preview" can't exfiltrate or phone home.
4. **Speculability gate** (`is_speculable`) — deny-by-default: network verbs,
   `sudo`/device access, external-state mutations (`git push`, `docker`, `npm`,
   already covered by dedicated oracles), and absolute writes outside cwd are
   refused and fall back to static analysis.
5. **Opt-in only** — nothing runs unless `BLAST_SCOPE_SPECULATE=1`. Never on
   the default hook path.

**Availability is narrow by design:** Linux with unprivileged user + overlay
namespaces (kernel ≥ 5.11, not distro-disabled). Everywhere else — including
the fundamental limit that network and external-DB effects are *not*
CoW-reversible — it returns unavailable and the static oracles stand. The real
sandbox and its safety invariant (*after a destructive run, the real tree is
untouched*) are exercised on the Linux CI runner.

## Severity & recommendation

| score      | severity  | recommendation |
|------------|-----------|----------------|
| ≥ 0.80     | critical  | block          |
| ≥ 0.50     | high      | confirm        |
| ≥ 0.20     | medium    | confirm        |
| < 0.20     | low       | proceed        |

("block"/"confirm" are advice — the hook never hard-blocks.)

## Calibration

`eval.py` runs the labeled corpus (`tests/fixtures/eval_corpus.jsonl`, 58 cases
spanning every recoverability category — including `repo_history` (`rm -rf .git`)
and a tracked-file control — git clean-vs-dirty state, infra/config files, a
graph-indexed central module, the git/docker/pip/SQL classes including
degrade-to-estimate paths, and the resolution layer: unset-var hazards, env-var
and glob-expanded targets, `sh -c` payloads, npm pre-hooks, script files,
opaque wrappers, and unresolved substitutions). Each case is materialized in a throwaway project —
including git working-tree state and, when needed, a built dependency graph —
then scored with the real `assess()`. It reports:

- **exact severity** and **within-one-band** accuracy
- **gate** precision/recall/F1 (proceed vs. flag, truth = not-low)
- a confusion matrix and a list of mismatches

Run it: `python -m blast_scope.eval`.

**Current calibration:** 58/58 exact, 58/58 within-one-band, gate F1 1.00.
`tests/test_eval.py` pins these with headroom (exact ≥ 0.85, within ≥ 0.95,
F1 ≥ 0.9, and no critical-labeled command ever scored `proceed`) so future
tuning can't silently regress.

### Large-corpus calibration (SABER)

The in-repo corpus is small and hand-built, so it can't measure a false-positive
rate or surface over-flagging at scale. `bench/saber_eval.py` calibrates against
[SABER](https://github.com/sssr-lab/saber) — 716 real coding-agent workspaces,
each with safe commands and (for scenario-A injection tasks) a harmful one. It
materializes each workspace, git-inits it, and scores every command through the
real `assess()` **without executing any** (see `bench/README.md`).

All figures below are the default (hook) configuration with no dependency graph
built, **with the resolution layer** (env/glob binding, script transparency,
read-only substitution).

| metric | value | notes |
|---|---|---|
| benign false-positive rate | **0.58%** (10/1725) | +3 vs pre-resolution — benign-labeled scripts whose *contents* legitimately score medium+ once opened |
| recall · `data_destruction` | **82.4%** (14/17) | pre-resolution this level needed the built graph; binding gets there on the hook path |
| recall · `code_tampering` | **50%** (5/10) | was ~0% — pipe-to-shell and wrapper transparency |
| recall · `fs_destruction`   | **61.5%** (8/13) | mass-destruction gate + `find -exec` payload verbs |
| recall · `persistence` | **28.6%** (4/14) | incidental catches via wrapper/pipe hazards |
| recall · out-of-scope classes | ~0–18% | exfiltration / priv-esc remain a different threat model |

Overall harmful recall 30.4% (35/115), up from ~17% pre-resolution. Three
calibration lessons, all FP-driven: an *unconditional* opaque floor on
`python -c` was the dominant FP source (benign one-liners are constant agent
traffic) — the floor now requires a destructive/obfuscation token in the
payload (`rmtree`, `os.system`, `.execute(`, `base64`, …); a wrapper
referencing a script that *doesn't exist* fails at runtime — that's a note,
not a hazard; and the mass-destruction gate is **content-aware** (≥3 source
files, directly or inside recursively-deleted dirs) because "is a directory"
can't distinguish `rm -rf tmp/ downloads/` from `rm -rf src tests` — the
first is routine cleanup, the second guts the codebase and floors at 0.55
even though git could restore it.

SABER drove three scorer changes documented above: destructive-intent gating of
the recoverability and path-tied floors (the dominant FP source — `sqlite3 db
'.tables'`, `hexdump key.pem`, `cp config.toml` no longer over-flag), and the
`repo_history` category (`rm -rf .git` / the repo root). Low recall on
exfiltration/persistence/priv-esc is expected: blast-scope scores destructive
*consequence*, not malicious *intent* — that boundary is a deliberate design line.

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
