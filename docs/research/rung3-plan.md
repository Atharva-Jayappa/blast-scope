# Rung 3 research plan — execution-grounded labels, calibrated P(harm)

**Status: PROPOSED — not started. No code until the go/no-go pilot passes.**
Synthesized 2026-07-07 from three independent deep-research passes (full reports
in this directory: [novelty](rung3_novelty.md), [methodology](rung3_methodology.md),
[validity](rung3_validity.md)).

---

## The verdict on novelty (as of 2026-07-07)

The niche **survives, narrowly**. No published system combines all three of:

1. per-command harm labels derived from **observed filesystem diffs** of actually
   executing the command (CoW speculative execution),
2. an **explicitly calibrated** P(harm) — not a binary gate, not a recall-tuned score,
3. **selective-prediction routing** (allow / ask / block, uncertainty → speculation/human).

Each ingredient exists alone; the conjunction is unclaimed. The three closest works,
all verified against their actual papers:

- **ClayBuddy (arXiv 2606.19380)** — owns "learned command classifier + probability +
  tiered routing." Its labels are a risk×intent heuristic; recall-tuned, never executes.
- **ProbGuard (arXiv 2508.00500)** — owns "calibrated risk learned from execution traces,"
  but for embodied agents over symbolic states, not shell/filesystem.
- **RedCode-Exec (arXiv 2411.07781)** — owns "executes risky code in Docker," but scores
  agent behavior against scenarios hand-labeled risky a priori; trains nothing.

**Therefore the paper must lean on label provenance + calibration, never on
"we built a learned command-risk model."**

## Scope constraints (violating any of these is the desk-reject list)

- **NOT** "harm labels for shell commands" — only *local filesystem side-effect harm for
  offline, idempotent, speculable commands*. The speculability gate structurally excludes
  network/sudo/DB/non-idempotent harm; the gate is reframed as the **abstention region**
  (non-speculable ⇒ ask, by policy) and **coverage is a headline number**.
- **NOT** "ground-truth harm" — only *observed effects* (measured) + an *explicit,
  published harm rubric* (authored, versioned, human-audited on 200–500 diffs with κ).
- **NOT** "first execution-grounded" (RedCode/SABER exist) — claim *scalable diff-derived
  labels* (no per-scenario hand-written oracles) *+ calibrated selective routing*.
- **NOT** calibrated-in-general — ECE reported **per distribution**; factory calibration
  does not transfer to agent traffic without recalibration, and the paper says so.
- **NOT** adversarial robustness — threat model is *non-adversarial agent mistakes*.
- **NOT** any SABER-derived headline for a model whose training touched SABER —
  **SABER is external eval only, forever** (same discipline as the bench/ split).
- **Label hygiene (non-negotiable):** a run that timed out, exited non-zero, hit a missing
  binary, or was nondeterministic across repeats **never yields "safe" — it yields
  `unknown` → ask**. All identified harness artifacts bias toward false-safe; this rule
  converts silent bias into honest coverage loss.

## The insurance policy (paper spine)

The trained router may not beat an LLM judge at routing — grounded and judge labels agree
on easy cases. The paper's spine is therefore the **judge audit**: *"LLM-judge harm labels
disagree with executed reality on X% of speculable commands, and in each disagreement we
show the diff proving the judge wrong."* That finding stands regardless of whether the
trained model wins; the calibrated router is the application built on top.

## Phase 0 — go/no-go pilots (run BEFORE building anything)

1. **Judge-vs-grounded disagreement pilot (~500 speculable commands).** Judge labels vs
   diff-derived labels; κ + hand-inspection of disagreements. Meaningful disagreement with
   grounded verifiably right → the paper has its spine. ~0% → **stop, no paper.**
2. **Speculability-coverage measurement.** Fraction of real agent-trajectory commands
   (SWE-rebench/OpenHands, SWE-chat dumps) that passes the gate. Sets the automation-value
   ceiling; itself a publishable measurement. If the speculable-AND-destructive slice is
   tiny, reconsider.
3. **Label-stability probe (~200 commands × 5 runs).** Test-retest diff agreement after
   canonicalization; % nondeterministic.
4. **Truncation/error audit.** Size of the `unknown` bucket (4s timeout, non-zero exit)
   on the pilot set — how much of the interesting tail is lost.
5. **Contamination check.** Verify zero overlap between SABER and training seeds.

Pilots 1–2 decide GO/NO-GO; 3–5 are cheap and ride along. All need only the existing
speculate.py harness on Linux (CI or WSL2) — no new engineering beyond scripts.

## Phase 1 — minimum viable paper (only after GO)

- **Corpus:** NL2Bash `data/bash` (MIT), filtered to gate-speculable verbs.
- **Workspaces:** parametric domain-randomized generator, 3–4 factors
  (git state × precious-file placement × target-dir population × import in-degree),
  stratified to ~20–30% harm rate at generation, importance-reweighted back to true base
  rate for all reported metrics.
- **Label:** rule-based binary **P(irreversible loss)** over diff + git state (deterministic,
  auditable); severity ordinal secondary; monetary value excluded (policy layer, not label).
  Human-annotated 200–500 stratified subset; report κ. Precedent: SWE-bench Verified.
- **Model:** engineered features (blast-scope already computes most) + GBT + Platt
  (positives too thin for isotonic). Small-LM arm deferred to v2 as the ablation.
- **Eval:** risk-coverage + AUGRC (Traub, NeurIPS 2024), per-region ECE (equal-mass bins)
  + Brier, explicit allow/ask/block operating points (block precision, allow missed-harm
  rate, ask rate). External eval: SABER data-destruction slice (held out).
- **Size:** ~10k–30k (command × workspace) pairs, ≥1–2k harm-positives. ~140 CPU-hours;
  1–2 days on a few parallel Linux workers. Sizing is **positives-bound**.
- **Ops:** ephemeral runners, no secrets, cgroup/disk limits, strict teardown
  (mass-executing untrusted commands is the real operational risk, not compute).

## Phase 2 — archival version (defer all of this)

Small-LM feature arm; agent-trajectory + honeypot + CI corpus layers; real-OSS workspace
bases; LLM-judge "value" study; severity-ordinal router.

## Publication path

NeurIPS 2026 E&D deadline passed (May 6). Plan: **arXiv preprint + safety workshop now**
(method at small scale) → **NeurIPS 2027 Evaluations & Datasets** archival (Croissant +
RAI metadata mandatory — desk-reject otherwise; hosting on HF/Zenodo with DOI; datasheet).
Alternates: USENIX Security / IEEE SaTML. Dual-use posture: open release + datasheet +
intended-use statement (no capability uplift — every LLM knows `rm -rf`; the novel asset
is defensive). Independent-researcher submission is fine everywhere (double-blind).

## Decisions locked with the project owner

| # | Decision | Choice |
|---|----------|--------|
| 1 | Claim scope | (a) honest filesystem/data-destruction scope; gate = abstention region |
| 2 | Label target | binary P(irreversible loss) primary, severity ordinal secondary, value excluded |
| 3 | SABER | external eval only, never trained on (incl. any LM arm) |
| 4 | Release | fully open + datasheet + intended-use |
| 5 | Venue | arXiv/workshop now → NeurIPS 2027 E&D |
| 6 | Compute | few parallel Linux workers (CI/WSL2); ~20k pairs realistic |
| 7 | Annotation | 200–500 subset; annotators TBD |

*(Table reflects the recommendations; owner sign-off pending — see session notes.)*
