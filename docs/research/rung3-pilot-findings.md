# Rung 3 Phase-0 pilot — findings & decision

**Date:** 2026-07-07 · **Status: complete. Recommendation: NO-GO on the data factory.**
Harness: [`bench/rung3_pilot/`](../../bench/rung3_pilot/). Plan: [rung3-plan.md](rung3-plan.md).

## Question

Before building the Rung 3 "data factory" (execute command corpora → derive harm
labels from observed diffs → train a calibrated P(harm) model), the pilot tested
its load-bearing premise cheaply: **do LLM-judge harm labels disagree with
execution-grounded labels often enough — and in the dangerous direction (judge
calls real harm "safe") — to justify grounding labels in execution?**

## Method

- **500 scenarios** (seed 11) across 10 strata, spanning obvious cases and a
  "hard" set whose effect needs multi-step simulation: glob expansion over the
  real tree, a delete target computed at runtime inside a script, a
  runtime-conditional delete, `find … -delete` over tracked files.
- **Grounded arm** — each command run in blast-scope's overlay CoW sandbox (real
  `speculate.py`, in a privileged Docker container; WSL2 was broken locally),
  harm label derived from the observed diff + `recoverability.classify_path`.
  A run that was gated, timed out, errored, or was nondeterministic across 3
  repeats yields `unknown`, never `safe`.
- **Judge arm** — DeepSeek-chat (~671B, strong) and Qwen-2.5-7B (a cheap gate,
  matching ClayBuddy's small-Qwen archetype), via OpenRouter, predicting the
  label from the command + a workspace view, no execution. Run in two regimes:
  **full** (judge sees file/script contents) and **listing** (names + git state
  only).

## Results (n=500; 492 comparable, 8 grounded-`unknown` excluded)

| judge | κ | disagreement | **under-read** (missed harm) | over-read (false alarm) | where |
|---|---|---|---|---|---|
| DeepSeek-chat | 0.86 | 6.3% | **0** | 31 | all `regenerable` |
| Qwen-2.5-7B | 0.68 | 13.8% | **0** | 68 | `regenerable` 47, `find_tracked` 21 |

- **Under-read is exactly zero for both models.** Neither ever called a
  genuinely harmful command "safe," across all 500 scenarios including the hard
  strata.
- **Every disagreement is over-read** — the judge flags a *recoverable*
  operation (`rm -rf build/dist/node_modules`, deleting a git-tracked file) as
  harm. The cheaper model over-flags more, but the error is always in the safe
  direction.
- **Full vs listing: 99% identical** (497/500). Hiding file/script contents
  barely moved the judge — filenames (`.env`, `prod.sqlite`, `node_modules`)
  carry the signal, so script-transparency is not where the action is.

## Interpretation

The pilot's original thesis — *execution-grounded labels catch dangerous
commands that judges **miss*** — is **refuted on this corpus.** Judges do not
miss filesystem harm; if anything they are over-cautious. Grounding therefore
buys **no safety uplift** here.

What grounding *does* correct is the judges' systematic **over-flagging of
recoverable operations**. But that value is already delivered by blast-scope's
existing static engine: the reason execution labels those cases `safe` is
[`recoverability.py`](../../src/blast_scope/recoverability.py) — the
regenerable-dirs table and the git-tracked check. **The current structural
recoverability signal already beats both LLM judges on the exact axis where they
fail.** A model trained on execution labels would, at best, re-learn what
`recoverability.py` already encodes. That removes the justification for the data
factory.

## Caveats (the result is only as strong as the corpus)

- **Synthetic, name-telegraphed, non-adversarial, filesystem-only.** Real agent
  commands are messier; this says nothing about network/exfil/persistence
  (structurally excluded by the speculability gate).
- **The one place under-read might still appear is obfuscated / hidden-effect
  commands** (`chr()`-built `python -c`, effects hidden from static reading).
  Not tested — but these are an *attack* pattern (prompt-injection), whose
  prevalence in the honest non-adversarial agent distribution the tool targets
  is ~zero, and which belong to a different threat model. Even a positive result
  there would be pinned to a rare tail, so the probe was judged low-value.
- **Grounded "truth" for the over-read strata partly rests on the tool's own
  recoverability rubric** (regenerable ⇒ safe). A human could contest specific
  weights; those disagreements are "judge vs the rubric's definition of
  recoverable," and would need human adjudication in a full study.

## Decision & what's banked

**NO-GO on the data factory + trained P(harm) model.** The pilot did its job — a
day and ~$0.35 of API spend instead of months on a false premise. Three things
carry forward:

1. **A real production bug fix** surfaced by the pilot:
   [`speculate.py` userxattr whiteouts](../../src/blast_scope/speculate.py) —
   `rm -rf <dir>` was silently unobservable in the overlay on WSL2 / rootless /
   hardened kernels (mknod whiteouts blocked in the userns). Independent of the
   research.
2. **A tight, honest measurement finding** — *LLM safety judges (strong and
   cheap) never under-read but systematically over-flag recoverable filesystem
   operations; a structural recoverability signal corrects it.* This reinforces
   the dependency-graph / structural-consequence positioning rather than chasing
   a learned model, and is publishable as a short note.
3. **The reusable judge-vs-grounded harness** for regression-checking that story.
