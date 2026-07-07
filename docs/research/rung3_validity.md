# Rung 3 "Data Factory" — Pre-mortem / Threats-to-Validity Review

Reviewer stance: hostile-but-fair. Date: 2026-07-07. Research-only; no code touched.

Contribution under review: execute command corpora (NL2Bash seed + agent trajectories +
SABER workspaces) inside blast-scope's overlayfs CoW speculation sandbox, record ground-truth
effect diffs, derive **harm labels** from the observed diffs, and train a small **calibrated
P(harm)** model with **selective-prediction routing** (allow / ask / block). Pitch: *first
execution-grounded harm labels + calibrated probability for shell commands.*

---

## TL;DR — the three threats most likely to kill this

1. **The label is silently redefined to "local-filesystem harm," and the most dangerous
   commands are structurally uninlabelable.** The speculability gate refuses network / sudo /
   device / non-idempotent commands, so the diff (and therefore every label) can only ever
   encode filesystem side-effects. `aws s3 rm`, `DROP TABLE`, `git push --force`, `curl … | sh`
   — the highest-consequence agent actions — cannot enter the dataset. If the paper says "harm
   labels for shell commands," one reviewer sentence sinks it. **FATAL unless the claim is
   scoped to "local filesystem side-effect harm for offline, idempotent, speculable commands."**

2. **Labels are "ground truth" only if the harness is faithful — and several harness artifacts
   bias the label toward calling catastrophic commands *safe*.** Timeout truncation (4 s cuts a
   large recursive delete → partial diff → "moderate"), missing-binary / non-zero-exit runs
   ("no effect observed" → "harmless"), and namespace codepath divergence (network absent → a
   `curl-then-rm` script takes the error branch → destructive branch never fires → "harmless")
   all point the same direction: **false-safe labels on exactly the commands that matter.** This
   is not noise; it is directional bias, and it is fatal to a *safety* label unless the harness
   emits `unknown` (not `safe`) whenever a run truncated, errored, or was nondeterministic.

3. **Train/eval contamination on the headline SABER number, plus a calibration claim that won't
   transfer.** SABER is used both as the benchmark that produces the 82.4% recall / 0.58% FPR
   headline *and* as a proposed training source. If any SABER workspace/command touches training,
   the headline is contaminated. Separately, calibration measured on an NL2Bash-seeded factory
   distribution is provably sensitive to P(X) and will not hold on 2026 agent traffic
   (long chains, heredocs, `python -c`). A "calibrated P(harm)" claim measured on the wrong
   distribution is the easiest thing in the paper to falsify.

The contribution **can survive all three**, but only if it is reframed from "we built a better
harm classifier" to "**we built an execution oracle that audits how wrong cheap labels
(LLM-judge / rules) are, and we route with calibrated confidence only where we can ground the
label.**" That reframing is cheap and is the paper's insurance policy (see Threat 6).

---

## Threat register

### T1 — Label validity: circularity, value-laden harm function, scope narrowing
**Severity: MAJOR (FATAL on the scope-narrowing sub-point if unaddressed).**

Reviewer's version:
> "Your 'ground-truth harm' is your own rubric applied to a diff produced by your own harness.
> The diff measures filesystem effects only, so 'harm' silently means 'local filesystem harm.'
> The commands whose harm is *not* filesystem-local are exactly the ones your gate refuses, so
> the definition of harm is decided by the tool's limitations, not by what harms users."

Sub-threats:
- **Circularity (features ↔ labels).** *Defensible.* The deployed scorer consumes
  **pre-execution static features** (tokens, flags, graph in-degree). The label comes from the
  **post-execution diff**. Features and labels are produced by different stages, so there is no
  trivial label leakage. State this explicitly — it defuses the naive circularity charge.
- **Circularity (harness ↔ grounding claim).** *Real.* The model is trained to predict
  `h(diff_harness(cmd))`, i.e. *what this harness would observe*, not *what happens on the
  user's machine*. Every harness artifact (T3) becomes systematic label error. You have
  "grounded on a simulator"; calibration is calibrated-to-simulator until proven on real fs.
- **Value-laden harm function.** `h(diff)` encodes what is "precious": is deleting
  `node_modules/` harm? `.git/`? one `src/*.py`? untracked scratch? These weights are authored,
  not discovered. Presenting `h()` output as "ground truth" launders subjectivity. Ground truth
  is only the **effect** (paths created/modified/deleted); the **harm** is a rubric.
- **Novelty contestable.** "First execution-grounded harm labels for shell commands" is weak
  against RedCode-Exec (NeurIPS'24, executes in Docker, adjudicates filesystem/process state via
  per-case oracle scripts) and SABER (adjudicates *final environment state*, HSR metric). Both
  are already execution-grounded. Honest novelty = **(a)** labels from a *generic effect-diff*
  rather than per-scenario hand-written oracles (scalable labeling), **(b)** a *calibrated
  probability* + selective-routing head. Claim that, not "first grounded."

Mitigation:
- Rename the artifact "**observed filesystem effects + an explicit, published harm rubric**,"
  not "ground-truth harm." Ship the rubric as a versioned function with documented weights and a
  small **human-audit** (e.g. 200 diffs hand-scored by 2+ people; report agreement with `h()`).
- Scope the harm claim to **local filesystem side-effect harm** in the title/abstract.
- Position vs RedCode/SABER as "scalable diff-derived labeling" not "first grounded."

Residual risk: the rubric's edge weights remain arguable; reviewers can always contest a
specific weight. Acceptable if weights are transparent and ablated.

---

### T2 — Coverage / censoring bias: the gate refuses the commands that most need judging
**Severity: MAJOR (→ FATAL if the paper reports full-distribution metrics from a censored set).**

Reviewer's version:
> "Agents most need judgement on `sudo`, network installs, force-pushes, DB writes — and those
> are precisely what your gate refuses. Your training distribution is the safe, boring subset;
> your calibration guarantee is meaningless on the region where decisions are hard."

Facts / gaps:
- The speculable set is **dominated by reads** (`grep/cat/ls/find` — harmless) plus a thin slice
  of *offline, idempotent, filesystem-local* destructive commands. The destructive-AND-speculable
  intersection — where the signal lives — may be small, and harm positives rare (class imbalance).
- **No public measurement** of what fraction of real agent shell commands is speculable. I could
  not find one; general-source material only confirms agents lean on `grep/sed/awk/cat` reads and
  gate destructive actions behind HITL. **Measuring this fraction is itself a contribution** and
  must be done before building the factory (see de-risking #2).
- Train-serve mismatch: the deployed static scorer runs on **everything**, including the censored
  region where you have **no labels** and no calibration data.

Mitigation (the strong one):
- **Reframe the gate as the abstention region.** Selective prediction already partitions into
  allow/ask/block. Make **non-speculable ⇒ automatic "ask/human"** by policy. Then the honest
  claim is: *"For the speculable region we emit calibrated P(harm) and can auto-route; everything
  else defers to the human by construction."* The censoring stops being a bias and becomes the
  system's coverage boundary — which is exactly how calibrated selective classification is
  supposed to behave. Report **coverage** (fraction auto-routed) as a headline number alongside
  selective risk.
- Report all metrics **conditioned on speculability**; never a single number over a censored set.

Residual risk: if the auto-routable fraction is small (e.g. mostly harmless reads + a few `rm`s),
the practical automation value is thin and a reviewer asks "so what do you actually save the
human from?" This is answerable only with the coverage measurement — run it first.

---

### T3 — Execution fidelity: the diff ≠ real-world effect
**Severity: MAJOR overall; the truncation / error / branch-divergence sub-threats are
FATAL-if-unaddressed because they bias labels toward false-safe.**

Reviewer's version:
> "A 4-second overlay run in a network-less namespace is not the user's machine. Any command that
> branches on the network, runs long, needs a missing binary, or hits an overlayfs corner case
> gets the wrong diff — and your label inherits the error."

Sub-threats and required handling:
- **Timeout truncation (4 s).** A large `rm -rf` / `find … -delete` is killed mid-way → partial
  diff → *under*-counts harm on the worst commands. **Rule: a run killed by timeout must be
  labeled `unknown`, never `safe`/`moderate`.**
- **Missing binary / non-zero exit.** Sandbox image lacks a helper → command errors → "no effect"
  → false-harmless. **Rule: non-zero exit or command-not-found ⇒ `unknown`, not `safe`.**
- **Namespace codepath divergence.** Network absent → `curl X && rm -rf $DIR` (or `python -c`
  that fetches then deletes) takes the error branch → destructive branch never executes →
  labeled harmless. The gate is meant to exclude these, but a script that *looks* idempotent can
  branch on network internally (`bash run.sh`, `python -c`). Static AST taint helps for literal
  `curl`/`wget` but not for opaque script bodies. **Residual: real; enumerate as a known failure
  mode and exclude opaque-script commands from `safe` labeling.**
- **Nondeterminism** (timestamps, PID-named temp files, RNG, races): same command → different
  diff. **Rule: run N times; if diffs disagree beyond a canonicalization step, flag
  nondeterministic and drop or down-weight. Report test-retest agreement as a label-quality
  metric.**
- **overlayfs vs real fs semantics.** Whiteouts (already handled per commit `b34eb7f`: rm -rf of
  a lower dir → one whiteout, not per-child), but hardlinks, xattrs, `mmap` writes, rename, and
  device nodes diverge. Enumerate known-divergent categories; exclude from the gate.

Precedents (use them; reviewers will):
- **binpash `try`** is explicitly *not* a sandbox — it allows network and is a human-in-the-loop
  *preview*, not a labeler. It never claims fidelity; it shows the diff and the human decides.
  Cite it as the honest baseline for "overlay diff ≠ verdict."
- **Yan, transactional sandbox (arXiv 2512.12806)** snapshots the **real** filesystem and rolls
  back, precisely to avoid namespace/codepath divergence. Reviewer: *"Why overlay-in-userns
  instead of snapshot-of-real-fs, which preserves the real codepath?"* Have an answer (isolation,
  no mutation of the live tree, network-off by design) and acknowledge the fidelity trade.
- **RedCode-Exec (4,050 cases)** uses **per-case hand-written evaluation scripts** over Docker
  filesystem/process state — a curated oracle, not a generic diff. Reviewer: *"Grounded
  benchmarks use per-scenario oracles for a reason; your generic diff→harm map is a weaker,
  noisier oracle."* Answer: scalability (generic diff labels thousands of unseen commands; curated
  oracles don't), and validate the generic map against a curated subset.

Mitigation (single most important fix): **the harness must never emit a "safe" label from a run
that truncated, errored, or was nondeterministic — those become `unknown` and route to "ask."**
This converts most fidelity failures from silent false-safe bias into honest coverage loss.

Residual risk: opaque-script branch divergence remains; some fraction of `safe` labels on scripts
will be wrong. Quantify it on a spot-checked sample.

---

### T4 — Distribution shift + contamination
**Severity: FATAL for contamination if SABER touches training; MAJOR for calibration transfer.**

Reviewer's version:
> "NL2Bash is 2018 human one-liners; 2026 agent commands are heredocs, chained `&&`, `python -c`.
> Calibration on your factory distribution is not calibration on deployment. And you cannot report
> a SABER headline for a model whose training set includes SABER."

Facts:
- **Contamination.** SABER is the eval producing 82.4% recall / 0.58% FPR *and* a listed training
  seed. Overlap ⇒ invalid headline. SABER is a curated benchmark (state-based HSR adjudication),
  likely hundreds of tasks — too small to both train on and hold out meaningfully.
- **Calibration transfer.** Literature is unambiguous: ECE/calibration is highly sensitive to
  P(X) under covariate shift; a temperature/isotonic map fit on factory data "may no longer suit"
  the deployment distribution. Calibrated-on-factory ≠ calibrated-on-agent-traffic.

Mitigation:
- **Hard partition:** SABER stays *external eval only*; zero SABER commands/workspaces in any
  training seed. Document the partition and publish the disjointness check. Train seeds =
  NL2Bash + agent trajectories only.
- **Report calibration on a held-out *agent-trajectory* distribution**, not just the factory.
  Recalibrate (temperature scaling) on a small labeled deployment sample; report ECE **per
  distribution** and the pre/post-recalibration gap. If calibration collapses off-factory, say so
  — an honest "calibration is local to the speculable in-distribution slice" is publishable; a
  false "calibrated P(harm) for agent commands" is a desk-reject waiting to happen.

Residual risk: agent-trajectory corpora are themselves a moving target; calibration will drift and
needs periodic refit. Frame as a maintained artifact, not a one-shot guarantee.

---

### T5 — Security / dual-use
**Severity: MINOR–MAJOR (dual-use is low; sandbox-escape-at-scale is operational; obfuscation
overclaim is MAJOR if the threat model is unstated).**

Reviewer's / ethics-chair's version:
> "You are publishing a curated corpus of destructive commands with measured effects, generated by
> mass-executing untrusted commands on CI. Justify the dual-use release and the operational safety."

Sub-threats:
- **Dual-use dataset.** *Low.* These are mundane shell commands (`rm -rf`, `chmod`, redirects),
  not novel exploits — the corpus teaches an attacker nothing new, and the framing is **defensive**
  (safer agents). Still, follow current norms (precedent: the 1,554-prompt validated malicious-code
  bank, arXiv 2605.03179; NeurIPS ethics guidelines): datasheet with intended-use limits, a code
  of conduct / gated access if any command is genuinely sensitive, takedown-on-request, no novel
  weaponization. Cheap to satisfy.
- **Sandbox escape at scale.** Mass-executing untrusted commands via unprivileged userns +
  overlayfs (both have CVE history) on shared CI runners = real blast radius. Mitigate with
  disposable microVMs / gVisor, network fully off, no secrets on the runner, ephemeral runners.
  Operational, not a paper-claim killer — but a reviewer will ask.
- **Obfuscation ceiling / overclaim.** A static pre-execution scorer cannot see a `python -c`
  payload built from `chr()`. If the paper claims "detects harmful commands," a reviewer writes a
  one-line evasion. **Scope the threat model to non-adversarial agent *mistakes*, not deliberate
  obfuscated attacks** — which is the honest and common case (agents mostly err, not attack).
  State it explicitly.

Residual risk: none serious if the threat model and distribution controls are stated.

---

### T6 — Baseline / evaluation attacks (where the paper lives or dies on its merits)
**Severity: MAJOR — the LLM-judge baseline can deflate the headline; but the paper is
survivable if restructured.**

Reviewers **will** demand:
- **LLM-judge baseline** (GPT/Claude scoring the same commands zero-shot). **This is the killer
  baseline.** If a frontier judge matches the trained calibrated model on allow/ask/block routing,
  the entire "execution factory" is unjustified: *"Why build a sandbox pipeline when the judge
  scores these as well and needs no execution?"*
- **Rule tools:** Shellfirm, dcg / deadly-command-guard, and blast-scope's own static scorer.
- **Ablations:** features-only vs learned; **grounded labels vs LLM-judge labels *as training
  signal*** — does grounding actually improve *downstream routing*, or only the *eval*?
- **Inter-method agreement:** Cohen's κ across grounded labels, judge labels, rule tools.

The honest danger: grounded labels may **not** beat judge labels for *training a router that only
sees static features* — both label sources agree on easy cases (>90%), and the disagreements are
rare edge cases that don't move aggregate routing metrics. If so, "train a better model with
grounded labels" dies.

**The result that makes the paper strong regardless:** use the grounded oracle to **audit the
cheap labelers.** "LLM-judge labels disagree with executed reality on X% of speculable commands,
and in the disagreements we *show the diff* proving the judge wrong (e.g. it called a truncating
`>` redirect harmless, or flagged a harmless `find` as dangerous)." Literature already shows judges
carry position/verbosity/self-preference bias and noisy ground truth (arXiv 2512.16041,
2604.16790) — a *grounded* disagreement measurement is a genuine, self-standing contribution.
**Structure the paper so its survival does not depend on the trained model winning at routing;**
the judge-audit finding is the spine, the trained router is the application.

Residual risk: if judge-vs-grounded disagreement turns out negligible (~0%), the paper has no
story. That is exactly why the disagreement pilot is the go/no-go (de-risking #4).

---

### T7 — Practical / solo-dev
**Severity: MINOR.**

- **Compute is not the blocker.** 10k commands × ~10 variants × ~5 s ≈ 140 CPU-hours — a weekend
  on one machine, trivially parallel on CI. Say so; don't let a reviewer assume it's a barrier.
- **Real cost is harness robustness at scale:** leaked mount namespaces, overlay disk fill, zombie
  processes. Mitigate with strict per-run teardown, cgroup CPU/mem limits, disk quotas, ephemeral
  runners.
- **Maintenance:** version + freeze the artifact, datasheet, Zenodo DOI, licence with intended-use.
- **Affiliation:** independent-researcher submissions are fine at NeurIPS D&B / ICLR / USENIX and
  safety workshops (double-blind neutralizes affiliation). Headwind is credibility, not
  eligibility; a **D&B or safety workshop** is the right first venue, and extra rigor (human audit,
  disagreement stats, per-distribution calibration) buys credibility a big lab gets for free.

Residual risk: sustaining a living dataset artifact solo. Mitigate by shipping it *frozen and
versioned*, not as a service.

---

## What the paper must NOT claim

1. **NOT** "harm labels for shell commands." Only "**local filesystem side-effect labels for
   offline, idempotent, speculable commands**."
2. **NOT** "ground-truth harm." Only "**observed filesystem effects** scored by an **explicit,
   published harm rubric**." Effects are measured; harm is authored.
3. **NOT** "calibrated P(harm) for agent commands" in general. Only on the **speculable,
   in-distribution slice**, with **coverage reported** and **ECE per distribution**.
4. **NOT** that it detects or prevents **adversarial / obfuscated** harmful commands. Threat model
   = non-adversarial agent mistakes.
5. **NOT** any **SABER headline number** for a model whose training touched SABER. SABER is
   external eval only.
6. **NOT** that the **trained model beats LLM judges at routing** unless shown on a held-out
   **deployment-distribution** set with the judge given equal information.
7. **NOT** "first execution-grounded labels." RedCode-Exec and SABER are already grounded; claim
   "**scalable diff-derived labels + calibrated selective routing**."
8. **NOT** any "safe/harmless" label emitted from a run that **truncated, errored, or was
   nondeterministic** — those are `unknown`.

---

## Cheapest de-risking experiments to run FIRST (in order)

1. **Judge-vs-grounded disagreement pilot (GO / NO-GO).** ~500 speculable commands; get an
   LLM-judge harm label and a grounded harm label; compute κ and hand-inspect disagreements. If
   disagreement is meaningful **and grounded is verifiably right**, the paper has its spine. If
   ~0%, stop — there is no story. Do this **before building the factory.**
2. **Speculability-coverage measurement.** Classify every command in the real agent trajectories
   as speculable vs gated; report the fraction. Answers T2 quantitatively and sets the ceiling on
   automation value / the size of the abstention region. Pure bookkeeping over existing corpora.
3. **Label-stability probe.** Run ~200 speculable commands 5× each; measure diff test-retest
   agreement after canonicalization; report % nondeterministic. Tells you whether labels are even
   stable before you trust any of them (T3).
4. **Truncation / error audit.** Of the speculable set, count runs that hit the 4 s timeout or
   exit non-zero. That bucket must be `unknown`; measuring its size tells you how much of the
   "interesting" (large-delete, long-running) tail you actually *lose* (T3).
5. **Contamination partition check.** Enumerate SABER commands/workspaces; verify zero overlap
   with NL2Bash + agent-trajectory training seeds; publish the disjointness. Cheap; removes the
   easiest desk-reject (T4).

Run #1 and #2 first: together they decide whether the contribution exists (#1) and whether it
matters at scale (#2), for a few days of work and no new engineering.

---

## Sources
- SABER — arXiv [2606.01317](https://arxiv.org/abs/2606.01317)
- Fault-Tolerant / transactional sandbox (Yan) — arXiv [2512.12806](https://arxiv.org/abs/2512.12806)
- RedCode / RedCode-Exec — arXiv [2411.07781](https://arxiv.org/abs/2411.07781)
- NL2Bash — arXiv [1802.08979](https://arxiv.org/abs/1802.08979)
- binpash/try — [github.com/binpash/try](https://github.com/binpash/try)
- LLM-as-a-judge reliability — arXiv [2512.16041](https://arxiv.org/html/2512.16041v1),
  [2604.16790](https://arxiv.org/html/2604.16790v1)
- Calibration under covariate shift — arXiv [2605.21552](https://arxiv.org/pdf/2605.21552),
  [2006.16405](https://arxiv.org/pdf/2006.16405); Calibrated Selective Classification
  [2208.12084](https://arxiv.org/html/2208.12084v2)
- Dual-use corpus norms — validated malicious-prompt bank arXiv [2605.03179](https://arxiv.org/pdf/2605.03179);
  [NeurIPS Ethics Guidelines](https://neurips.cc/public/EthicsGuidelines)
