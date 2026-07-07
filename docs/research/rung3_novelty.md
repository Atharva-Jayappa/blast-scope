# Rung 3 Novelty Verification — "Execution-verified labels → calibrated P(harm) for shell commands"

**Date of verification:** 2026-07-07
**Scope:** Verify/refute the ~2026-07-02 report claim that no published system trains a *calibrated P(harm) model on execution-grounded labels for shell commands* (everyone else labels from LLM-judge trajectories or risk×intent heuristics).
**Method:** Fetched actual arXiv abstracts/HTML + GitHub READMEs. Confidence tags: **VERIFIED** = read the actual source; **REPORTED** = search-result / secondary-summary level.

---

## TL;DR verdict

The niche **survives, narrowly**. As of 2026-07-07 I found **no published system** that (a) derives *per-command* harm labels from *observed filesystem/state diffs of actually executing the command*, (b) trains an *explicitly calibrated* P(harm) (not a binary or recall-tuned gate), and (c) uses *selective-prediction routing to a speculation branch*. All three ingredients exist **separately** in prior work, and two competitors get uncomfortably close:

- **ClayBuddy (2606.19380)** already ships a *learned two-stage command-risk classifier that emits a probability and routes allow/prompt/terminate* — but its labels are a synthetic **risk×intent heuristic**, it is **not execution-grounded**, and it is **not calibrated** (Stage-1 tuned to 100% recall). This is the strongest reviewer citation against "a learned command-risk classifier with tiered routing."
- **ProbGuard (2508.00500)** already *learns a probabilistic model (DTMC) from execution traces and predicts P(unsafe state)* with threshold intervention — but for **embodied/driving/household agents, not shell commands**, over **symbolic abstract states, not filesystem diffs**, with **no per-command harm labels**.
- **RedCode-Exec (2411.07781)** already *executes risky code in Docker and grounds outcomes in observed effects* — but scenarios are **hand-labeled "risky" a priori**, execution grounds **agent-behavior outcome** (did the pre-designed risky effect fire), not a **novel per-command harm label**, and it **trains nothing**.

**Bottom line:** "a learned classifier for command risk with routing" is **NOT novel** (ClayBuddy). "probabilistic risk learned from execution traces" is **NOT novel** (ProbGuard). "execution-grounded outcome scoring in a sandbox" is **NOT novel** (RedCode). The **defensible novelty is the specific conjunction**: *execution-diff-derived per-command harm labels → an explicitly calibrated P(harm) → selective prediction that routes uncertainty to CoW speculation*, for **shell commands**. No paper claims that conjunction. The novelty must lean on **label provenance + calibration**, NOT on "we built a learned command-risk model."

---

## 1. Competitor verification table (claimed vs. verified)

| # | System (report's ID) | ID resolves? | Report's claim | What I verified | Labels / ground truth actually from | Calibrated (prob, not binary)? | Learns from executed effects? | Conf. |
|---|---|---|---|---|---|---|---|---|
| 1 | **ClayBuddy** — 2606.19380 | ✅ Yes | 2-stage learned cascade (Qwen 0.8B → Haiku), risk×intent labels | *ClayBuddy: A Framework, Evaluation, & Mitigation of Coding Agent Failures* (Kenneth Ge, Andre Assis). Command classifier = **Stage 1 fine-tuned Qwen3.5-0.8B emits a high-risk probability (fast gate) → Stage 2 Haiku-4.5** decides allow/prompt/terminate. Claim **accurate**. | **Risk×intent heuristic**: "label BLOCK if command was both risky AND unrelated to user intent, ALLOW otherwise." Commands from Claude Code transcripts + synthetic user queries. **No execution, no human, no diffs.** | ❌ No — Stage-1 tuned to "100% recall on validation set"; emits a score but recall-optimized, not calibrated | ❌ No | **VERIFIED** |
| 2 | **Sentinel / Atlassian** — 2604.12177 | ✅ Yes | CoW speculative-apply on a knowledge graph; Allow/Block/Clarify; 92.99% acc | *Policy-Invisible Violations in LLM-Based Agents* (Jie Wu **@ Atlassian**, Ming Gong). Introduces **PhantomPolicy** benchmark + **Sentinel**: forks the org **knowledge graph**, applies the action as a mutation, checks **7 declarative graph invariants** → Allow/Block/Clarify. Acc **93.0%** vs 68.8% DLP baseline (report's 92.99% ≈ correct; "CoW" is a loose analogy — it's a *graph fork*, not filesystem CoW). | **Manual human review of 600 model traces** (5 frontier models); trace-level human labels (manual review flipped 5.3% of case-level labels). | ❌ No — binary decision via graph invariants | ⚠️ Speculative-executes the **abstract graph**, not the filesystem; no learning | **VERIFIED** |
| 3 | **Agent-Sentry** — 2603.22868 | ✅ Yes | Provenance-aware argument allowlist learned from executions | *Agent-Sentry: Bounding LLM Agents via Execution Provenance* (Sequeira, Damianakis, Iqbal, Psounis). Runtime defense; **deterministic allowlist over sensitive argument values** learned from "prior legitimate executions" + **provenance of each function's arguments**; LLM judge for uncertain cases. Blocks 94.3% injections, permits 95.1% legit. Claim **accurate**. | **Prior legitimate executions** define the benign envelope (anomaly-style); no harm labels from diffs | ❌ No — deterministic checks + LLM-judge fallback | ⚠️ Learns *what's normal* from executions, but **not harm labels from observed effects**; targets injection/argument anomaly, not filesystem consequence | **VERIFIED** |
| 4 | **Boyang Yan** — 2512.12806 | ✅ Yes | Transactional real-shell sandbox, 100% interception, 14.5% overhead | *Fault-Tolerant Sandboxing for AI Coding Agents: A Transactional Approach…* (Boyang Yan). **Policy-based interception layer + transactional filesystem snapshot** (rollback). 100% interception of high-risk cmds, 100% rollback, **14.5% overhead (~1.8s)/transaction**. Claim **accurate**. | **Rule/policy-based** interception; no dataset, no labels | ❌ No — binary policy interception | ❌ No — snapshots for *rollback*, not for label generation; no learning | **VERIFIED** |
| 5 | **dcg / Destructive Command Guard** (GitHub Dicklesworthstone) | ✅ Yes | Rule + AST hook | *destructive_command_guard*: PreToolUse-style hook; **regex packs (50+) + tree-sitter AST** + heredoc scanning + context classification (comment vs executed). Claim **accurate**. | **Rules/patterns**, static only, pre-execution | ❌ No — binary block/allow JSON verdict | ❌ No — decides *before* execution, never observes effects | **VERIFIED** |

**Discrepancy notes:** All five IDs resolved. My first-pass abstract read of ClayBuddy missed the classifier internals (the abstract only says "customizable command classifier"); the **body text confirms the report's 2-stage Qwen→Haiku + risk×intent description**. Sentinel's "CoW on a knowledge graph" is more precisely a **speculative graph fork + invariant check**, and its accuracy is 93.0% (not 92.99% — trivial rounding). Atlassian affiliation **confirmed** (Jie Wu).

---

## 2. New-since-May-2026 findings (nothing claims the white space)

| Paper / ID | Date | What it does | Does it claim the niche? |
|---|---|---|---|
| **ProbGuard** — [2508.00500](https://arxiv.org/abs/2508.00500) *(Wang, Poskitt, Wei, Sun)* | Aug 2025 | Proactive runtime monitor; abstracts executions into **symbolic states**, **learns a DTMC from execution traces**, estimates **P(reach unsafe state)**, intervenes above a user threshold. | **Closest on "calibrated/probabilistic from execution traces."** BUT embodied/driving/household domains, **not shell**; symbolic states **not filesystem diffs**; no per-command harm labels; no speculate-branch routing. **VERIFIED** |
| **AgentTrust** — [2605.04785](https://arxiv.org/abs/2605.04785) *(Chenglin Yang)* | May 2026 | Runtime interception between agent & tools; verdict **allow/warn/block/review**; shell deobfuscation normalizer + RiskChain + **cache-aware LLM-as-Judge**; ~93% on shell-obfuscated payloads. | No trained calibrated model; **LLM-judge + rules**, not execution-diff labels. Prominently shell-focused but binary-ish verdict. **VERIFIED** |
| **Malicious Agent Skills in the Wild** — [2602.06547](https://arxiv.org/abs/2602.06547) | Feb 2026 | Large-scale empirical study; ephemeral Docker sandbox execution to build a **ground-truth malicious-skill dataset** (honeypot creds, network monitoring). | Execution-based dataset, but **malware-family/forensic** labels, not per-shell-command calibrated P(harm). **REPORTED** |
| **Trace-Economic Underwriting** — [2606.16465](https://arxiv.org/abs/2606.16465) | Jun 2026 | Insurance/underwriting over 10,036 incidents; fields include **blast radius, rollback quality, write/delete/execute**. | Actuarial risk pricing over incident traces, **not** a per-command P(harm) from diffs; adjacent vocabulary only. **REPORTED** |
| **SoK: Trust-Authorization Mismatch** — [2512.06914](https://arxiv.org/abs/2512.06914) | Dec 2025 | Systematization; defines **"blast radius" = cascading effects within the dependency graph** (network/semantic/data-flow reachability). | Conceptual framing that *matches blast-scope's thesis* but is a SoK — no model, no labels. Useful to cite **as support**, and a reviewer may say "the concept is already named." **REPORTED** |
| **What Breaks When LLMs Code** — [2605.30777](https://arxiv.org/abs/2605.30777) / **Overeager Coding Agents** — [2605.18583](https://arxiv.org/abs/2605.18583) | May 2026 | Characterization studies of operational safety failures / out-of-scope actions. | Measurement papers, no learned harm model. **REPORTED** |
| **SafeDream** — [2604.16824](https://arxiv.org/abs/2604.16824) | Apr 2026 | Safety **world model** for early jailbreak detection; turn-level labels via **majority vote of 3 classifiers** (HarmBench/GPT-4o/MD-Judge). | "World model" + proactive, but jailbreak-content labels via LLM-classifier vote, **not execution diffs**. **REPORTED** |

**SABER follow-up / citations:** SABER authors (Qi Hu et al.) have **no follow-up training a P(harm) model**; SABER itself only *evaluates* models. Neighboring June-2026 characterization work (2605.30777, 2605.18583) does not build a learned harm scorer. No citation of SABER claims the execution-diff-label → calibration niche.

---

## 3. Benchmark label-methodology landscape

**Critical question: does ANY published dataset derive per-command harm labels from observed filesystem/state diffs of actually running the command? → No.** The closest, RedCode-Exec, executes but scores *pre-labeled* risky scenarios.

| Benchmark / ID | Ground truth produced by | Executes the command? | Per-command harm label from observed diff? |
|---|---|---|---|
| **SABER** — [2606.01317](https://arxiv.org/abs/2606.01317) | **Rule-based** on extracted **state deltas** + hand-authored per-task unsafe-action sets (harmful cmd patterns Qt, tool patterns Pt); **LLM judge as *auxiliary* only** (never downgrades a rule hit). Evaluates 13 models on 716 tasks; **trains nothing**. | ✅ Yes (final env state) | ⚠️ Partial — checks state deltas against **pre-authored** harmful patterns; not a learned label. **VERIFIED** |
| **RedCode-Exec** — [2411.07781](https://arxiv.org/abs/2411.07781) (NeurIPS'24) | **25 vuln seeds × 30 variants / 8 domains, hand-crafted a priori**; per-case Docker script categorizes **Rejection / Execution-Failure / Attack-Success** by whether the *specific designed effect* fired. | ✅ Yes (Docker) | ⚠️ **No** — grounds *agent-behavior outcome* against **pre-labeled** risky scenarios; the "risky" label is by construction, not derived from the diff. **VERIFIED** |
| **OS-Harm** — [2506.14866](https://arxiv.org/abs/2506.14866) (NeurIPS'25) | 150 **human-annotated** o4-mini traces (binary success + binary safety + first-violation step); **LLM judge** auto-grades (F1 ~0.76–0.79 vs humans). | ✅ Runs in OSWorld | ❌ No — human/LLM-judge trace labels. **VERIFIED** |
| **AgentHarm** — 2410.09024 | **110 hand-curated malicious tasks** (11 harm cats) + benign twins; grades *compliance*. | Partially | ❌ No — human-curated task harmfulness. **REPORTED** |
| **ToolEmu** — 2309.15817 | **LM-emulated** tool execution + **LM-based safety evaluator**; no real execution. | ❌ Emulated | ❌ No — LM-judge on emulated traces. **REPORTED** |
| **R-Judge** — [2401.10019](https://arxiv.org/abs/2401.10019) | 569 multi-turn records, **human-annotated** safety labels + risk descriptions; used to test LLM-judge risk awareness. | ❌ Retrospective on records | ❌ No — human annotation. **REPORTED** |
| **Agent-SafetyBench** — 2412.14470 | Structured agent traces across 8 risk cats; **LLM-based classifiers** for safety scoring. | ⚠️ Sandboxed interactive envs | ❌ No — LLM-judge scoring. **REPORTED** |
| **InjecAgent** — 2403.02691 | Single-turn injection scenarios; **deterministic** attack-success detection. | ❌ Simulated tool output | ❌ No — injection-success rule check. **REPORTED** |
| **AgentDojo** — [2406.13352](https://arxiv.org/abs/2406.13352) | 97 tasks × injection tasks; **programmatic ground-truth eval functions** verify task + injection success (ASR). | ✅ Real tool envs | ❌ No — programmatic *task/attack success*, not command harm labels. **REPORTED** |
| **τ-bench / τ²-bench** — 2406.12045 | Binary reward comparing **final DB state + comms** against **hand-authored** ground-truth annotations. | ✅ Real DB state | ⚠️ State-comparison but against **hand-authored** goals; not harm labels. **REPORTED** |

Pattern: the field labels harm via **(a) human annotation** (OS-Harm, R-Judge, Sentinel), **(b) LLM judge** (ToolEmu, Agent-SafetyBench, OS-Harm aux), **(c) hand-authored rules/patterns** (SABER, AgentDojo, InjecAgent, τ-bench), or **(d) execution against pre-labeled risky scenarios** (RedCode-Exec). **None** runs an *arbitrary* command in a sandbox and *derives* that command's harm label from the *observed diff* to then *train a calibrated scorer*.

---

## 4. Surviving novelty statement + anticipated reviewer objections

### Strongest defensible novelty framing
> *We present the first pipeline to (1) execute arbitrary shell-command corpora in randomized synthetic workspaces under a copy-on-write speculative-execution harness, (2) derive **per-command ground-truth effect labels directly from the observed upper-layer filesystem/state diff** (not from a priori risky-scenario tags, LLM-judge trajectory scores, or risk×intent heuristics), and (3) train a **calibrated P(harm)** model with **selective-prediction routing** (confident → allow/block, uncertain → ask/speculate) for AI-agent shell commands.*

The novelty is a **conjunction of label provenance + calibration + selective routing** — each ingredient exists alone; the combination is unclaimed. Do **not** frame the contribution as "a learned command-risk classifier" (ClayBuddy owns that) or "probabilistic runtime risk from traces" (ProbGuard owns that).

### Anticipated objections & the closest prior work a reviewer would cite

1. **"ClayBuddy already learns a command-risk classifier that emits a probability and routes allow/prompt/terminate."** — True and this is the #1 threat. **Rebuttal:** ClayBuddy's labels are a synthetic *risk×intent heuristic*, its Stage-1 score is *recall-tuned not calibrated*, and it never executes commands. Our contribution is the **execution-diff label source** and **proper calibration/selective prediction** — orthogonal to "a classifier exists." Report reliability curves / ECE vs. ClayBuddy-style heuristic labels to make the delta concrete.

2. **"ProbGuard already predicts calibrated P(unsafe) learned from execution traces."** — **Rebuttal:** different domain (embodied, not shell), abstract *symbolic states* not *filesystem diffs*, no per-command harm labels, no CoW-diff label factory, no speculate branch.

3. **"RedCode-Exec already grounds harm in real Docker execution."** — **Rebuttal:** RedCode's "risky" is hand-assigned a priori; execution scores *agent behavior* (attack-success) against **pre-labeled** scenarios; it trains **no** model. We *derive* the label from the diff of *arbitrary* commands and *train a calibrated scorer* — a different object.

4. **"Sentinel already does speculative execution + Allow/Block/Clarify."** — **Rebuttal:** Sentinel speculates over an **abstract org knowledge graph** with **human-labeled** traces and **hand-authored invariants**; no filesystem diffs, no learning, no calibration.

5. **"SABER/AgentDojo/τ-bench already read final state."** — **Rebuttal:** they compare state against **hand-authored** per-task conditions to grade *models*; none produces a *training corpus* of diff-derived per-command harm labels feeding a calibrated scorer.

6. **"'Blast radius' is already a named concept (SoK 2512.06914; OWASP 2026)."** — **Rebuttal:** those are conceptual/governance framings; none operationalizes it as an execution-grounded, calibrated, per-command label pipeline. Cite them as *motivation*, not as scooping.

### Residual risk
The riskiest sliver is **ClayBuddy**: a reviewer can argue "learned command classifier + probability + tiered routing is done." The novelty *only* holds if the paper foregrounds **(a) execution-diff-derived labels** and **(b) calibration quality + selective prediction under distribution shift** as the measured contributions, ideally with a head-to-head showing diff-grounded labels beat heuristic/LLM-judge labels on calibration (ECE/Brier) and selective-risk (risk-coverage / AURC) curves. Framed that way, the contribution is defensible and unpublished as of 2026-07-07.
