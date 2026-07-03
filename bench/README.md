# bench — external-corpus calibration

The in-repo eval (`python -m blast_scope.eval`) calibrates against a small,
hand-built corpus (`tests/fixtures/eval_corpus.jsonl`). This directory holds
calibration against **larger, external** corpora that are too big — or too
differently-licensed — to vendor.

## SABER

[SABER](https://github.com/sssr-lab/saber) (Benchmarking Operational Safety of
LLM Coding Agents in Stateful Project Workspaces, arXiv:2606.01317) is a
716-task benchmark. Each task ships a materializable workspace plus ground
truth: `expected_safe_commands` (should be allowed) and, for scenario-A
injection tasks, a literal harmful command in `injection.payload` (should be
flagged). That maps directly onto blast-scope's `assess()`.

`saber_eval.py` reports two honest, large-sample views:

- **benign false-positive rate** — across every task's `expected_safe_commands`
  (~1725 real safe commands, in real project states). The number blast-scope
  most needs to defend: an advisory that cries wolf gets ignored.
- **harmful recall, per category** — on the scenario-A subset where a concrete
  harmful command is cleanly recoverable. Reported per category *on purpose*:
  it shows exactly where blast-scope's coverage ends. blast-scope models
  destructive *consequence* (filesystem/data destruction, git, docker, pip,
  SQL); it does **not** model supply-chain injection, exfiltration, persistence,
  or privilege escalation — those are a different threat model (malicious
  content, not destructive consequence), out of scope by design. High recall on
  `data_destruction` / `fs_destruction` and low recall elsewhere is the expected,
  honest result: the per-category table marks that boundary on purpose — it is
  not a backlog of unfinished work.

### Get the dataset (not vendored)

SABER is ~360 MB and **CC-BY-4.0**, so it is not committed here. Clone it
anywhere and point the harness at `dataset/data/tasks.jsonl`:

```bash
git clone --depth 1 https://github.com/sssr-lab/saber.git
# Windows note: the full checkout hits MAX_PATH in baselines/results/. Only the
# dataset is needed — if checkout aborts, restore just it:
#   git -C saber restore --source=HEAD -- dataset
```

### Run

```bash
# Full corpus (git-initializes each workspace — the realistic default).
python bench/saber_eval.py --tasks saber/dataset/data/tasks.jsonl --json bench/saber_baseline.json

# Test the dependency-graph signal too (slower — builds a graph per task):
python bench/saber_eval.py --tasks saber/dataset/data/tasks.jsonl --index

# Quick slice while iterating:
python bench/saber_eval.py --tasks saber/dataset/data/tasks.jsonl --limit 50
```

### Modeling choices (read before trusting a number)

- **git-init by default.** Real coding-agent workspaces are git repos with the
  existing project committed, so a project file is `tracked_clean` (recoverable),
  not `untracked`. Without this, *every* file looks unrecoverable and the
  false-positive rate is meaningless. `--no-git` disables it.
- **graph off by default.** The baseline runs the fast hook-path (`auto_index=
  False`); SABER workspaces are small, so the dependency-graph signal is minor
  here. `--index` turns it on to measure the graph's contribution (the focus of
  the SCIP/binding-resolution work).
- **commands are never executed.** Harmful strings are parsed and scored as
  data, exactly like `tests/fixtures/`. The harness only materializes a
  workspace and calls the pure `assess()`.
- **Windows fidelity gap.** SABER uses absolute Linux paths; the harness remaps
  the project/home tree under a sandbox, but attacks on unmaterialized system
  paths (`/etc`, `/usr`) score against an absent path and under-score. Recall on
  host-system categories is therefore a lower bound.

### Attribution

SABER is © its authors, licensed CC-BY-4.0. This harness reads its published
task data; it does not redistribute it. Cite the SABER paper (arXiv:2606.01317)
when reporting numbers derived from it.
