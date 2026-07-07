# Rung 3 Data Factory — Methodology Design

**Contribution under design:** execution-grounded harm labels for AI-agent shell commands, produced by running large command corpora in randomized synthetic workspaces via blast-scope's overlayfs CoW speculative sandbox, extracting ground-truth effects from the upper-layer diff, deriving harm labels, and training a small calibrated P(harm) model with selective-prediction routing.

**Date:** 2026-07-07. **Author role:** research methodologist. **Scope:** methodology only, no code.

**Claim marking convention:** `[VERIFIED]` = I fetched and read the primary source; `[REPORTED]` = search-result / secondary summary only.

---

## TL;DR — top recommendations

1. **Corpus: layered, not single-source.** Primary benign backbone = NL2Bash `data/bash` (MIT `[VERIFIED]`). Realism layer = shell commands mined from public agent trajectory dumps (SWE-rebench-OpenHands-trajectories, SWE-chat, Open-SWE-Traces) — this is the true "AI-agent command" distribution NL2Bash lacks. Destructive-tail enrichment = Cowrie/Q-Cowrie honeypot logs. Benign-ops layer = CI scripts from the `gigawork` GitHub Actions corpus. **Hold SABER out entirely** as clean external eval (never train on it).

2. **Workspace: parametric domain-randomized generator over a small factorial of state covariates** (git state × precious-file placement × target-dir population × graph in-degree), seeded partly by real permissively-licensed OSS snapshots. This is procedural-content-generation / domain-randomization applied to safety labels. Stratify/oversample the rare catastrophic corner (Balanced Domain Randomization logic) because harm is a low-base-rate event.

3. **Label: rule-based P(irreversible loss), computed deterministically over the sandbox diff + git state, validated against a human-annotated stratified subset.** The sandbox's exact created/modified/deleted paths are the novel ground-truth asset (like SWE-bench's execution-derived FAIL→PASS labels — the reviewer-accepted precedent). Keep monetary "value" *out* of the label (it's unmeasurable and would make the label indefensible); push it to a documented policy layer. Add a severity ordinal as a secondary target. Use LLM-judge only as an auxiliary/validation signal, never as the primary label (2025-26 judge-reliability literature is damning).

4. **Model: engineered-features + gradient-boosted trees + Platt/beta calibration as the primary baseline**, with a fine-tuned small-LM arm as the headline ablation. Route via **selective classification** (two thresholds → allow/ask/block), report **risk-coverage curves + AUGRC** (Traub et al., NeurIPS 2024) and **per-region ECE + Brier** (standard ECE is misleading here — the low-P region that matters is under-weighted). Ballpark **~10k–30k execution-labeled pairs with ≥1–2k harm-positives** (achieved via oversampled generation, then reweighted to true base rate) to get ECE < 2% with usable CIs in the operating region.

5. **Publication: NeurIPS Evaluations & Datasets track is the archival target, but the 2026 deadline (paper May 6, 2026) has passed `[VERIFIED]` — aim NeurIPS 2027 D&B, de-risked by an arXiv preprint / safety-workshop paper now.** Croissant metadata with core + Responsible-AI fields is mandatory (desk-reject otherwise) `[VERIFIED]`. Dual-use is real but *mild* (destroying files is not a capability uplift; the novelty is defensive) — a datasheet + intended-use/ethics statement suffices; no need to gate release.

**Single biggest methodological risk to resolve first:** the sandbox's deny-by-default speculability gate refuses network/sudo/device/non-idempotent commands. That means the factory *structurally cannot execution-ground* the exfiltration / persistence / privilege-escalation / network-outbound harm classes — only filesystem/data-destruction. Be honest and scope the claim to "execution-grounded harm labels for **filesystem/data-destruction** commands," or find a non-execution oracle for the rest (see Open Questions).

---

## Axis 1 — Command corpora

### Options considered

| Corpus | Size | License | Realism for "AI-agent commands" | Contamination risk |
|---|---|---|---|---|
| **NL2Bash** (Lin et al., LREC 2018) | ~9.3k NL–bash pairs; 8,090 train / 609 dev / 606 test; 102 utilities, 206 flags `[VERIFIED]` | **Code GPLv3; `data/bash` separately MIT (since 2020-04-24)** `[VERIFIED]` | Medium. One-liners answering NL questions — skews to "clever" `find`/`xargs`/`awk` pipelines, under-represents mundane `rm`/`mv`/`git`/`cp` that agents actually emit. | High for LM feature-extractor (StackOverflow-sourced, in every LLM's pretraining). Low as a *label* source — the labels are new. |
| **Agent trajectory dumps** — SWE-rebench-OpenHands-trajectories (HF, Dec 2025), SWE-chat (~6k sessions, 200+ repos, 355k tool calls), Open-SWE-Traces | Large, multi-turn | Per-dataset; SWE-chat/Open-SWE derived from permissive repos with PII/secret redaction `[REPORTED]` | **Highest** — literally emitted by coding agents (OpenHands, Claude-Code-style scaffolds). The distribution you actually care about. | Distribution is real but must be de-duped; some overlap with SWE-bench repos. |
| **Terminal-Bench** (ICLR 2026) | v1: 89 hand-crafted tasks, 16 categories; v2 exists `[REPORTED]` | Open-source benchmark `[REPORTED]` | High but small; each task is a Dockerized NL instruction + oracle solution. | It's a *benchmark* — treat like SABER, hold out if you'd report on it. |
| **InterCode-Bash** | 1,879 bash commands / 9,187 NL2Bash pairs, Dockerized `[REPORTED]` | Derived from NL2Bash (MIT data) | Medium (same NL2Bash provenance) | Same as NL2Bash. |
| **Cowrie / Q-Cowrie honeypot logs** | Q-Cowrie: 248,338 attack sessions/3 wks; Kaggle 2-wk Azure capture; HF `palisaderesearch/LLM-Honeypot-Logs`; one combined study = 617 Linux cmds `[REPORTED]` | Varies (Kaggle/HF terms; Cowrie tool is BSD) | Low for *benign agent* distribution, but **excellent for the destructive/malicious positive class** (real `rm -rf`, `dd`, `chattr`, history-wiping, `:(){ :|:& };:`) that agent traces rarely contain. | Attacker distribution ≠ agent distribution → use for positive-class enrichment, flag the shift. |
| **CI scripts — `gigawork` GitHub Actions corpus** | 160k+ commit histories, 32k+ repos, 1.5M+ workflow versions; Zenodo DOI 10.5281/zenodo.10259013 `[REPORTED]` | Public research release (Zenodo) | Medium-high for *benign ops* (build/test/deploy/`rm` of build dirs) | Low; mine `run:` steps for shell. |

### Recommendation

**A layered corpus with explicit provenance tags per command:**

1. **Benign backbone (clean license, easy):** NL2Bash `data/bash` (MIT). Use for the bulk of the benign class and for utility/flag coverage.
2. **Agent-realism layer (the important one):** mine `run:`/tool-call shell strings from **agent trajectory dumps** — this is the only source that matches the deployment distribution (an agent typing commands into a workspace). Vet each dataset's license individually; SWE-chat and Open-SWE-Traces advertise permissive provenance + PII redaction `[REPORTED]`.
3. **Destructive-tail enrichment (positive class):** honeypot logs, tagged as distribution-shifted.
4. **Benign-ops layer:** `gigawork` CI `run:` steps.
5. **External eval, never trained on:** SABER (716 tasks, CC BY 4.0 `[VERIFIED]`), optionally Terminal-Bench.

**De-dup across sources by normalized command AST** (blast-scope's parser already produces structured intent), not by raw string, to avoid `rm -rf ./x` vs `rm  -rf  ./x` leakage.

### Justification & the two contamination senses

- **Contamination sense A (benchmark leakage):** don't train on any corpus you'll report numbers on. SABER and Terminal-Bench are public and near-certainly in frontier-model pretraining, so if the LM-feature arm is used, treat SABER purely as held-out external validation.
- **Contamination sense B (pretraining leakage into the LM feature extractor):** NL2Bash/SABER commands are in LLM pretraining. This is *fine* for this project because **the novelty is the execution-grounded label, not the command text.** The commands can be maximally public; the (command × state) → harm mapping is what's new. Say this explicitly in the paper to preempt "isn't this all in GPT already" reviews.
- Realism justification: NL2Bash alone would make reviewers rightly ask "do these look like agent commands?" The agent-trajectory layer is the direct answer and is a 2025-26 asset that didn't exist when NL2Bash-based work was published.

---

## Axis 2 — Workspace generation

### The core problem

Label = f(command, **workspace state**). `rm -rf ./logs` is harmless in an empty dir and catastrophic if `./logs` holds the only copy of production data. To get labels that generalize, the generator must sample the **joint (command × state) distribution**, not just commands.

### Options considered

- **SABER-style hand-authored JSON workspace specs** (directory skeleton, file contents, git state, permissions, DBs) — 716 workspaces `[VERIFIED]`. Realistic, but hand-authoring caps volume and coverage; SABER templates come from prior benchmarks + CVEs + practitioner seeds with LLM-assisted detail synthesis `[VERIFIED]`.
- **Fully synthetic minimal projects** (like blast-scope's own `tests/fixtures/sample_project` with known import structure) — deterministic, good for controlled edge cases, low realism.
- **Real OSS repo snapshots** sampled from permissively-licensed GitHub — high realism, but you don't control the state factors and precious-file placement.
- **Procedural / domain-randomized generation** — the RL-safety analogue.

### Prior art (execution-grounded / procedural environments)

- **Procedural content generation (PCG) in RL**: train on randomized levels, test on held-out levels sharing state/action space — the standard generalization protocol `[REPORTED]`. Directly transferable: workspaces = "levels."
- **Domain randomization**: uniformly randomize environment parameters so the policy/model generalizes; known failure mode is that models **over-focus on common domains and neglect rare ones** `[REPORTED]`. **Balanced Domain Randomization (BDR)** reweights by context rarity `[REPORTED]` — this is *exactly* your harm-base-rate problem: catastrophic workspace states are rare, so uniform sampling drowns them out.
- **SABER's four workspace-validation criteria** (causal specificity, local-safe-path inferability, executable harm, balanced coverage) `[VERIFIED]` are a ready-made rubric to borrow for generator QA.

### Recommendation

**A parametric domain-randomized generator over a small factorial of state covariates, layered on partly-real, partly-synthetic bases, with stratified oversampling of the catastrophic corner.**

Randomize (each a documented covariate you can later condition the label analysis on):

- **git state:** clean / dirty / staged-only / untracked-heavy; has-remote vs no-remote; ahead/behind/pushed. (Determines reversibility.)
- **precious-file placement:** only-copy DB (`.sqlite`/`.db`), secrets (`.env`/`.pem`/`credentials`), uncommitted work, backup-present vs backup-absent.
- **target-directory nature:** empty / populated / symlinked / nested / near a mount boundary.
- **graph reachability:** target file's import in-degree (blast-scope's own dependency-graph signal — the project's moat feature; vary 0 → high).

Base layers:
- **(a) Real OSS snapshots** (permissive licenses) for realism of the "surrounding project."
- **(b) Parametric mutation** of git/precious-file state layered on top of (a).
- **(c) Fully synthetic minimal projects** for controlled edge cases and for the graph-in-degree sweep.

**Sampling design:** stratified so the harm-positive corner is over-represented at generation time (e.g., force precious-file placement in ~20-30% of instances), then **importance-reweight to the true base rate** at evaluation. Cite BDR as the principled justification.

### Justification

Uniform randomization would produce a dataset that is ~all-benign (base rate of harm in random workspaces is tiny), starving the positive class and the calibration bins that decide block/ask. The factorial-covariate design gives (i) coverage of the joint distribution, (ii) covariates to condition on for analysis ("recall stratified by git-state"), and (iii) a principled reweighting path back to the real base rate. Seeding from real repos answers the "are these realistic projects?" reviewer objection that pure-synthetic generation invites; SABER's small hand-authored set is your realism anchor / sanity check, not your volume source.

---

## Axis 3 — The harm label (the hardest problem)

### Decompose into two steps

1. **Effect extraction (already solved by the sandbox — the novel asset):** the overlayfs upper-layer diff yields *exact* created/modified/deleted paths + byte deltas. This is **measured ground truth**, deterministic and reproducible. This is what nobody else publishes.
2. **Harm derivation:** harm = f(effect, workspace). An observed deletion is not yet "harm" — harm depends on what was lost, its recoverability, and its value.

### Options considered

- **(a) Rule-based harm function over the diff.** Deterministic, auditable, cheap, reproducible. E.g., `irreversible_loss = ∃ deleted path P such that P was (untracked ∨ staged-only ∨ in a no-remote repo) ∧ not reconstructable ∧ not in a backup dir`. Composable sub-signals: bytes destroyed, tracked?, pushed?, secret-like?, only-copy?, in-src-tree/high-in-degree?
- **(b) Human annotation of a subset.** High-trust but doesn't scale; needed to *validate* the rule function.
- **(c) LLM-judge over the diff.** Grounded on real effects (much better than judging the raw command), but reintroduces judge noise: 2025-26 literature documents **position bias, self-preference/family bias, prompt-surface sensitivity, and "when judgment becomes noise" validity failures**; raw agreement overstates ability (must use chance-corrected κ, judge panels, Fleiss' κ + CIs) `[REPORTED]`.
- **(d) Hybrid:** rule-based primary; human + judge on a stratified subset to validate and to tune rule thresholds; report agreement.

### Precedent reviewers accept (labels from measured outcomes, not annotation)

- **SWE-bench / SWE-bench Verified**: correctness labels derived *purely from execution* — apply patch, run FAIL_TO_PASS + PASS_TO_PASS tests; instances excluded if the ground-truth patch doesn't produce the transition `[REPORTED]`. This is the canonical, reviewer-blessed "labels from execution, human-verified subset" pattern. Cite it as the methodological precedent.
- **AgentHazard**: 2,653 instances, taxonomy → instantiation → **execution-based filtering + human review** `[REPORTED]`. Same pattern in the agent-safety domain.
- **SABER**: harm detected via task-specific unsafe-action sets *plus global safety properties* checking for destructive filesystem changes/exfil/unauthorized-access from the final state `[VERIFIED]` — i.e., outcome-derived, not annotation of intent.

### Recommendation

**Rule-based P(irreversible loss) as the primary ground-truth label, computed deterministically over the sandbox diff + git state, validated against a ~200–500-instance stratified human-annotated subset (report Cohen's κ). Add a severity ordinal as a secondary target. Use LLM-judge only as an auxiliary signal for the hard-to-rule "value" component, with a judge panel + chance-corrected agreement — never as the primary label.**

**Target formulation (recommend one, justified):**

- **Primary target = binary `P(any irreversible loss)`.** Irreversibility is (i) the operationally decisive quantity for an allow/ask/block router (you can undo a reversible action; you cannot undo an irreversible one), and (ii) **objectively measurable** from diff + git/remote/backup state. This is the defensible label.
- **Secondary target = severity ordinal** (`none < reversible-modification < reversible-deletion < irreversible-nonprecious < irreversible-precious`) for richer analysis and for a graded router.
- **Reject expected-loss (monetary) regression** as the ground-truth label. Value is subjective/unmeasurable; baking it into the label makes it indefensible and non-reproducible. Push "value" into a **configurable, documented policy layer** downstream of the P(harm) model. (You can still *study* value via the optional LLM-judge, reported separately as noisy.)

### Justification

The whole pitch is "execution-grounded, not judge/heuristic." A rule function over *measured effects* is grounded and auditable; recoverability is a function of observable state (tracked/pushed/backed-up), so the rule is not a heuristic guess about intent — it's a deterministic readout of consequence. The human-validated subset + reported κ is precisely how SWE-bench Verified and AgentHazard defend their labels. Choosing irreversibility as the target keeps the label objective; choosing severity-ordinal as secondary hedges without compromising the primary. Keeping value out is what makes the dataset survive review.

---

## Axis 4 — Model + calibration

### Feature representation (and the headline ablation)

- **Arm 1 — engineered features + gradient-boosted trees (recommended primary baseline):** command AST/verb, flags, recursive, target count; workspace covariates (git state, precious-file flags, target-dir nature); **graph in-degree of targets** (the project's differentiator). GBT/logistic is interpretable, data-efficient (critical at low positive count), and directly calibratable. blast-scope already computes most of these.
- **Arm 2 — fine-tuned small LM** over `(resolved command string ⊕ serialized workspace summary)`. This is the "does a learned representation beat hand features?" ablation the pitch needs.
- **Arm 3 — late fusion** of both.

Start with Arm 1: it de-risks calibration (fewer params → less overfitting on a thin positive class) and gives an interpretable rationale (aligns with blast-scope's existing "rationale" contract).

### Calibration methods

- **Platt scaling / beta calibration** — parametric, data-efficient; **recommended default** because the positive class is small and isotonic overfits on few positives.
- **Isotonic regression** — nonparametric; *beats Platt on ECE/Brier with enough data* (statistically significant in benchmark studies) `[REPORTED]`, but needs volume. Use only once positive count is large.
- **Temperature scaling** — for the LM arm.
- **Conformal / calibrated selective classification** — for distribution-free guarantees on the router (Fisch et al. 2022, *Calibrated Selective Classification*, trains a selector so accepted predictions stay calibrated `[REPORTED]`; conformal risk control for bounding the block-region false-alarm or allow-region miss rate `[REPORTED]`).

### Selective-prediction routing (allow / ask / block)

The router **is** a selective classifier. Frame via **risk-coverage** (El-Yaniv & Wiener 2010, *On the Foundations of Noise-free Selective Classification*, JMLR — formalized the risk-coverage trade-off `[REPORTED]`; **Chow's rule (1970)** = optimal reject threshold given an abstain cost `[REPORTED]`; **SelectiveNet**, Geifman & El-Yaniv, ICML 2019 — end-to-end train prediction + selection with an integrated reject head `[REPORTED]`).

Two thresholds `τ_low < τ_high` on `P(harm)`: below `τ_low` → **allow** (auto), above `τ_high` → **block**, in between → **ask** (defer to human). Set them by a **target risk in the block region** (block-region precision) and a **target coverage** (auto-allow fraction), i.e., a cost-sensitive Chow's-rule operating point.

### Evaluation

- **Proper scoring rules:** Brier + log-loss for probability quality (always report Brier alongside ECE).
- **ECE with its pitfalls:** binning choices (count, equal-width vs equal-mass, norm, class-conditioning) can *reorder* which calibration method looks best `[REPORTED]`. Standard ECE also **over-weights the crowded high-confidence bins** and under-resolves the low-P region that decides allow/ask. Mitigations: use **equal-mass bins**, **report per-region ECE** (especially the operating region), drop bins with ≤5 samples, and pair ECE with Brier + reliability diagram.
- **Risk-coverage curve + AUGRC:** Traub et al., NeurIPS 2024 (*Overcoming Common Flaws in the Evaluation of Selective Classification Systems*) — **AUGRC = "average risk of undetected failures,"** meets 5 requirements standard AURC/AUROC fail, and *reordered method rankings on 5/6 datasets* `[REPORTED]`. This is the right summary metric for "how much undetected harm slips through the allow gate."
- **Report the router operating points explicitly:** block-region precision, allow-region false-negative (missed-harm) rate, ask-rate/coverage.

### Sample-size back-of-envelope (target: ECE < 2% in the low-P operating region)

- **The binding constraint is the number of positives (harm events), not total N**, because base rate is low.
- Per-bin accuracy SE ≈ √(p(1−p)/n_bin). In the low-P region (p≈0.02), n_bin ≈ 500 → SE ≈ 0.6% → comfortably < 2% at ~3σ. In the worst-case p≈0.5 region, hitting <2% needs n_bin ≳ 2,500. Across ~10–15 equal-mass bins ⇒ **order 10k–30k labeled pairs total**.
- But you need **enough positives per decision-region bin**: at a true 2–5% base rate, 10k pairs gives only ~200–500 positives — thin. Fix via the **oversampled generation** from Axis 2 (drive the *training/labeling* harm rate to ~20-30% by construction), then **importance-reweight** calibration metrics back to the true base rate.
- **Recommendation: ~10k–30k execution-labeled (command × workspace) pairs, of which ≥1–2k are harm-positive.** Feasible: ~4s/command sandbox → ~15/min single-thread → ~20k in 1–2 days on a handful of parallel CI/WSL2 workers.

### Justification

GBT-first is the data-efficient, interpretable, calibratable choice for a thin positive class; the LM arm is the required "learned beats engineered?" ablation. Platt-before-isotonic follows directly from the small-positive-count regime. Risk-coverage + AUGRC + per-region ECE + Brier is the defensible metric stack — AUGRC because a generic AUROC hides exactly the "undetected harm through the allow gate" quantity you care about; per-region ECE because standard ECE is provably manipulable and mislocated for low-base-rate decisions. Sizing is positives-bound, which reframes the whole data-collection budget around *generating enough catastrophic workspaces*, not enough commands.

---

## Axis 5 — Dataset/benchmark publication standards

### What the venues require in 2026

**NeurIPS 2026 — track renamed "Evaluations & Datasets" (E&D)** `[VERIFIED]`:
- **Croissant machine-readable metadata, core + Responsible-AI (RAI) fields, mandatory**; missing RAI metadata is flagged in review and **non-compliance justifies desk rejection** `[VERIFIED]`. An online RAI editor + Croissant validator are provided `[VERIFIED]`.
- **Hosting** on a dedicated ML site — Dataverse, Kaggle, Hugging Face, or OpenML (bespoke allowed); datasets >4GB need an inspectable sample `[VERIFIED]`.
- **Code required at submission when the primary contribution is a reusable executable artifact** — a *data generator* explicitly qualifies `[VERIFIED]`.
- Accessible to all reviewers/ACs/SACs at submission **without contacting the PI**; double-blind default (single-blind option for non-anonymizable datasets) `[VERIFIED]`.
- **Timeline for 2026: abstract May 4, full paper May 6, 2026 (AoE) — already past** `[VERIFIED]`. Next archival window is **NeurIPS 2027 E&D**.
- Datasheet (Gebru et al., *Datasheets for Datasets*: motivation, composition, collection, uses, distribution, maintenance) `[REPORTED]`; DOI comes free via the hosting platform (Zenodo/Dataverse/HF).

**Croissant** (MLCommons; DMLR workshop 2024) = 4 layers (metadata inc. RAI, resources, structure, ML semantics); **Croissant-RAI** extends Data Cards / Datasheets and is integrated by HF/Kaggle/OpenML `[REPORTED]`.

### Dual-use / ethics (this dataset catalogs destructive commands)

- The dataset is genuinely dual-use — it's effectively a recipe linking *commands* to the *workspace states that make them maximally catastrophic*. But it is a **mild** case: destroying files is not a capability uplift (every LLM already knows `rm -rf`), and the *novel* asset — the (command × state) → irreversible-harm mapping — is **more defensive than offensive** (its purpose is a guardrail scorer).
- Precedent handling `[REPORTED]`: responsible/coordinated disclosure norms (Coordinated Disclosure of Dual-Use Capabilities); release *code-for-defense* rather than a weaponizable artifact; state intended use. USENIX Security '26 now **requires open artifact sharing at submission** `[REPORTED]`.
- **Recommendation:** release openly with (i) a clear dual-use statement, (ii) an intended-use clause (defensive scoring / agent guardrails), (iii) a datasheet + ethics/broader-impact section. No gating needed — nothing here is a genuine uplift, and gating would undercut the reproducibility the D&B track demands. Contrast this explicitly with bio/cyber-exploit datasets to preempt the reviewer's dual-use reflex.

### Venue tiering for a solo researcher

- **Tier A (archival, highest bar):** NeurIPS / ICLR **Datasets & Benchmarks / E&D** track. Needs polished generator, Croissant+RAI, scale, external eval. Target **NeurIPS 2027**.
- **Tier B (security/safety):** **USENIX Security '26** (artifacts required at submission; Aug 12–14, 2026, Baltimore) `[REPORTED]`; **IEEE SaTML** (top AI-security venue) `[REPORTED]`; or a **NeurIPS/ICLR/ICML safe-AI / RegML workshop** for a fast, low-risk first landing.
- **Recommendation:** put the *full* dataset on the Tier-A path (NeurIPS 2027 E&D), but **de-risk now with an arXiv preprint + a safety-workshop paper** establishing the method (execution-grounded harm labels) on a smaller corpus. The **method is the novelty and is workshop-publishable at small scale**; scale + polish for the archival submission.

---

## Minimum viable paper (MVP) — build first vs defer

**The moat already exists:** the overlayfs CoW speculative sandbox with exact upper-layer diffing. Rung 3 is "wrap it in a labeling pipeline + a calibrated model."

### Build first (the MVP)
1. **One corpus, cleanest license:** NL2Bash `data/bash` (MIT), filtered to the *filesystem/data* verbs the sandbox can actually execute under its speculability gate.
2. **One procedural workspace generator** with 3–4 randomized factors (git state × precious-file placement × target-dir population × graph in-degree), stratified to ~20-30% harm rate.
3. **Rule-based `P(irreversible loss)` label** over the diff + git state, **validated on a 200–500-instance human-annotated stratified subset (report κ).**
4. **Engineered-features + GBT + Platt baseline**, with **risk-coverage / AUGRC + per-region ECE + Brier**, and explicit allow/ask/block operating points.
5. **Hold SABER out** as external eval; report cross-benchmark generalization (train on synthetic, test on SABER's data-destruction slice).
6. **~10k–20k labeled pairs** to start (positives-bound sizing).

### Defer
- Fine-tuned small-LM arm (Arm 2/3) — headline ablation, but adds a pretraining-contamination story; add for the archival version.
- LLM-judge "value" component and severity-ordinal / expected-loss regression.
- Agent-trajectory + honeypot + CI corpus layers (add for realism in v2 once the pipeline is proven on NL2Bash).
- Real-OSS-repo base layer at scale.
- Cross-OS / Windows; anything outside the speculability gate (network/sudo/persistence/priv-esc) until an oracle exists for it.

---

## Open questions for the project owner (decisions needed before coding)

1. **Speculability-gate scope (the big one).** The sandbox refuses network/sudo/device/non-idempotent commands, so the factory can only execution-ground **filesystem/data-destruction**. Do you (a) scope the paper's claim to that class honestly, or (b) add a *non-execution oracle* (dry-run oracles / static resolution) for exfil/persistence/priv-esc — accepting those labels are weaker? This bounds the entire contribution and must be decided first.
2. **Label target lock-in.** Confirm **`P(irreversible loss)` binary primary + severity ordinal secondary**, with monetary value excluded from ground truth (pushed to policy layer). Hard to change after generation.
3. **SABER hold-out discipline.** Agree to never train on SABER (keeps it as clean external eval) — even the LM arm?
4. **Compute budget** for the factory run: how many parallel Linux/CI/WSL2 workers? (Sets whether 20k or 200k pairs is realistic.)
5. **Release posture on dual-use:** fully open (recommended) vs gated? Affects hosting choice and ethics statement.
6. **Venue + timeline:** NeurIPS 2026 E&D deadline (May 6) has passed. Confirm the plan = arXiv/workshop now → NeurIPS 2027 E&D archival? Or push for USENIX Security '26 / SaTML instead?
7. **Human-annotation labor:** who annotates the 200–500 validation subset, and against what rubric? (Needed to defend the rule-based labels.)

---

## Source index

- NL2Bash — [repo](https://github.com/TellinaTool/nl2bash), [README (license) `[VERIFIED]`](https://github.com/TellinaTool/nl2bash/blob/master/README.md), [arXiv 1802.08979](https://arxiv.org/abs/1802.08979)
- SABER — [arXiv 2606.01317 `[VERIFIED]`](https://arxiv.org/html/2606.01317), repo github.com/sssr-lab/saber (CC BY 4.0)
- Terminal-Bench — [arXiv 2601.11868](https://arxiv.org/html/2601.11868v1), [ICLR 2026 PDF](https://openreview.net/pdf/417ac3236de7dbf3fc3414c51754dd239271663e.pdf)
- InterCode — [arXiv 2306.14898](https://arxiv.org/abs/2306.14898)
- Cowrie / honeypot — [Q-Cowrie](https://link.springer.com/article/10.1007/s10207-026-01221-5), [HF LLM-Honeypot-Logs](https://huggingface.co/datasets/palisaderesearch/LLM-Honeypot-Logs), [Kaggle Cowrie](https://www.kaggle.com/datasets/xmlyna/cowrie-honeypot)
- Agent trajectories — [SWE-rebench-OpenHands-trajectories](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories), [SWE-chat](https://arxiv.org/html/2604.20779), [Open-SWE-Traces](https://arxiv.org/pdf/2606.16038)
- gigawork GitHub Actions corpus — [MSR 2024](https://dl.acm.org/doi/10.1145/3643991.3644867) (Zenodo 10.5281/zenodo.10259013)
- SWE-bench Verified (execution labels) — [OpenAI](https://openai.com/index/introducing-swe-bench-verified/)
- AgentHazard — [arXiv 2604.02947](https://arxiv.org/abs/2604.02947); SafeAgentBench — [arXiv 2412.13178](https://arxiv.org/abs/2412.13178)
- LLM-judge reliability — [Reliability without Validity](https://arxiv.org/pdf/2606.19544), [When Judgment Becomes Noise](https://arxiv.org/html/2509.20293v1)
- Selective prediction — [El-Yaniv & Wiener 2010 (JMLR)](https://www.semanticscholar.org/paper/On-the-Foundations-of-Noise-free-Selective-El-Yaniv-Wiener/bcd842c0e6e731f347523d774b089cdf21d9e8f1), [SelectiveNet ICML 2019](https://proceedings.mlr.press/v97/geifman19a.html), [Fisch Calibrated Selective Classification](https://people.csail.mit.edu/fisch/publications/), [AUGRC / Traub NeurIPS 2024](https://arxiv.org/abs/2407.01032), [Conformal Risk Control](https://arxiv.org/pdf/2208.02814)
- Calibration — [Calibration Meets Reality](https://arxiv.org/html/2509.23665v1), [Understanding ECE](https://arxiv.org/html/2501.19047v2), [Benchmark Study on Calibration](https://arxiv.org/pdf/2308.11838)
- Publication standards — [NeurIPS 2026 E&D CfP `[VERIFIED]`](https://neurips.cc/Conferences/2026/CallForEvaluationsDatasets), [NeurIPS 2026 RAI metadata `[VERIFIED]`](https://blog.neurips.cc/2026/05/04/responsible-ai-metadata-requirements-for-the-evaluations-and-datasets-track-neurips-2026/), [Croissant arXiv 2403.19546](https://arxiv.org/abs/2403.19546), [Datasheets for Datasets], [USENIX Security '26 CfP](https://www.usenix.org/conference/usenixsecurity26/call-for-papers), [IEEE SaTML 2026](https://satml.org/program/)
- Domain randomization / PCG safety — [Balanced Domain Randomization](https://www.mdpi.com/2076-3417/14/21/9710)
- Dual-use — [Coordinated Disclosure of Dual-Use Capabilities](https://www.aimodels.fyi/papers/arxiv/coordinated-disclosure-dual-use-capabilities-early-warning)
