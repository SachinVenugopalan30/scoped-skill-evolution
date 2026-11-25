# Pre-registration: Scoped Skill Evolution, Final Experiments

**Project.** Scoped Skill Evolution for Code Review Agents (CSE 572, Group 22).
**Repository.** `scoped-skill-evolution`.
**Version.** 1.0, pre-registered **2026-11-23**, before any final-run data were produced.
**Model (frozen).** `qwen36-27b`, served by ASU Voyager at `https://openai.rc.asu.edu/v1`, used for the
**reviewer, optimizer, and repair agent**.
**Purpose.** Fix the analysis plan before the final runs so that the locked test partition is scored once,
against decisions that were not chosen after seeing test results.

Any decision not written here is an implementation detail. Any change to a decision written here is recorded
in [Amendments](#17-amendments) with its date, reason, and whether it was made before or after the freeze.
Test results are never used to choose or revise a condition, threshold, or metric.

---

## 1. Research question and hypotheses

**RQ.** Under repository shift, does organizing evolved review skills into global + PR-type-scoped tiers
improve held-out c-CRAB pass rate compared with a single global skill library, when both use the same
validation gate?

**H1 (primary, directional).** On the locked test partition, comment-level pass rate (errors as failed) is
higher for **scoped gated** than for **global gated**.

**H2 (secondary).** Scoped gated achieves a larger net gain per accepted edit and fewer regressions per
accepted edit than global gated over the evolution rounds.

**H0.** No difference beyond selection noise. The primary test is **two-sided**; H1 is the predicted
direction, not a justification for a one-sided test.

The comparison of interest is scoped vs global, both gated. Static is a reference point, not a
treatment arm.

---

## 2. Frozen model and infrastructure

| Item | Frozen value |
|---|---|
| Reviewer / optimizer model | `qwen36-27b` via ASU Voyager (`openai/qwen36-27b`) |
| Repair agent model | `qwen36-27b` through the local LiteLLM proxy alias `qwen36-27b` (`hosted_vllm/`) inside Claude Code |
| Decoding | temperature 0; thinking disabled (`chat_template_kwargs.enable_thinking=false`) for Qwen calls |
| Repair transport | local LiteLLM proxy on `127.0.0.1:4000`, started and stopped by the harness; ASU key stays on the host |
| Dataset | c-CRAB public release, submodule pinned at `856dfa3` |
| Docker platform | `linux/amd64` (emulated on Apple Silicon) |
| Harness | `harness.py` at the frozen commit; `HARNESS_VERSION` unchanged during scoring |
| Review output | at most 5 findings per PR |
| Diff cap | 120,000 characters of `patch_to_review` (~30k tokens) |

Every scored run must be traceable to the frozen commit and to the library hashes recorded in
`results/FROZEN.json` (Section 14). The model, endpoint, and proxy route are not changed after the freeze.

---

## 3. Data, splits, and exclusions

### 3.1 Census (reproduced from the release)

| Stage | PRs | Comments |
|---|---|---|
| 0. Full SWE-CARE test split | 671 | 1313 |
| 1. Comment filter | 410 | 595 |
| 2. Docker image available | 410 | 595 |
| 3. Tests verified fail→pass | 339 | 485 |
| 4. Resolved by the repair agent (used here) | 184 | 234 |

67 repositories contribute to stage 4.

### 3.2 Repository-disjoint splits

Repositories are assigned whole to a partition with fixed seed **2685**, using only repository size and PR
type mix, never outcomes. The search matches the proposal targets exactly, and each partition contains all
three PR types (bug_fix, feature, other).

### 3.3 Eligibility exclusions (fixed before sampling, outcome-independent)

A PR is eligible iff it has a prebuilt Docker image and its patch is at most `reviewer.MAX_DIFF_CHARS` =
120,000 characters. The rule is applied identically to all partitions and all conditions, before sampling:

* **no image:** one PR (`python__cpython-104029@f78e70e`, selection);
* **oversize:** eight PRs (dev 3, selection 2, test 3), whose diffs cannot be passed to the reviewer under
  the frozen cap.

| Partition | Split PRs / comments / repos | No image | Oversize | **Eligible PRs / comments / repos** | Role |
|---|---|---|---|---|---|
| development | 92 / 112 / 25 | 0 | 3 | **89 / 109 / 25** | fit skills, optimizer feedback |
| selection | 37 / 47 / 20 | 1 | 2 | **34 / 42 / 17** | gate only |
| test | 55 / 75 / 22 | 0 | 3 | **52 / 70 / 21** | locked scoring only |

The final static baseline and every evolution condition use the full eligible development and selection
partitions (or the same seeded nested prefix, documented with the run). No PR is moved between partitions.

The four test-partition PRs whose tests are JavaScript are **retained** and scored exactly as released; they
are not specially handled (Section 16, deviation D5).

---

## 4. Conditions

| Condition | Library | Gate | Skills retrieved |
|---|---|---|---|
| `static` | empty | n/a | none |
| `global` | global scope only | calibrated net-gain gate | global scope only |
| `scoped` | global + three PR-type scopes | same calibrated net-gain gate | PR-type scope first, then global |
| `scoped` ungated | global + three PR-type scopes | none (ablation, only if time) | same as scoped |

All conditions use the same reviewer, repair agent, Docker images, retrieval budget, dev/selection samples
(same seed and nesting), and round schedule. The only intended difference between `global` and `scoped` is
skill organization. `scoped` ungated differs from scoped gated only in whether the gate can reject an edit.

---

## 5. Review and repair measurement

* The reviewer sees PR title, body, and the truncated diff, plus retrieved skills. **No gold comment, test
  code, or outcome is ever placed in the reviewer prompt.**
* A finding is applied by the repair agent (Claude Code) inside the PR's Docker image.
* A PR **passes** iff **all** Stage-4 tests for that PR pass after repair.
* A **comment** passes iff its own Stage-4 test passes after applying the review's findings. Comment-level
  outcomes are derived from the same repair run; they are not independent repairs.
* Infrastructure errors (Docker failure, proxy failure, timeout, empty reviewer output after retries) give
  `passed = None`. Errors are **excluded from the gate** and counted as **failed** in the primary metric.
* Images are pulled per PR and deleted by exact tag after the round; this is the cleanup policy for all
  final runs.

---

## 6. Skill retrieval (frozen)

* Budget: **400 tokens** of skill text per review (same for global and scoped).
* `static`: no skills.
* `global`: only `global` skills.
* `scoped`: `bug_fix` / `feature` / `other` skills for this PR's type first, then `global` skills.
* Ordering: **newest-first within each scope group** (highest skill id first).
* Deduplication: identical or normalized-duplicate skill texts are kept once.
* Cap: at most **6** skills in the prompt.
* Overflow: a skill that would exceed the token budget is skipped, and later, smaller skills may still fit
  (skip-and-continue). The same budget and ordering rule apply to both gated conditions.

---

## 7. Sanitizer guarantees

Feedback to the optimizer is built from an **allowlist**, never by deleting fields from a richer record.
For each non-errored development PR the optimizer sees only:

* the PR type (`bug_fix`, `feature`, `other`);
* the reviewer's own finding headers and contents, after cleaning;
* the PR-level outcome as `passed` / `total` reference comments.

Cleaning replaces file paths with `[path]`, removes line references, redacts instance ids, repository and
file names, and caps text at 600 characters. Before every optimizer call, `assert_clean` rejects the exact
payload if it contains an instance id, repository name, changed file path, gold comment text, or test code.
The optimizer never sees diffs, test code, gold comments, per-comment identities, selection results, or test
results. Selection outcomes are used only by the gate, not as optimizer feedback.

---

## 8. Optimizer and feedback aggregation

* One optimizer call per round, made **after** evaluating the current library on the development partition.
* **Feedback aggregation:** all non-errored development PRs are combined into one payload, in a fixed order
  (seeded development order), each as `{pr_type, findings, passed, total}`. No per-PR outcome is attributed
  to a specific comment.
* **`MAX_EDITS = 1`**: at most one edit is proposed per round (add, replace, or delete). This differs from
  the pilot's 2 (deviation D3).
* Constraints: `add` needs a valid scope and ≤60 words; `replace`/`delete` need an existing skill id; at most
  60 words of skill text; invalid edits are dropped; an empty edit list means "no edits" and is recorded as a
  rejection (not an acceptance).
* In `global`, `add` scope must be `global`. In `scoped`, `add` scope is one of `global`, `bug_fix`,
  `feature`, `other`.
* The candidate library is the current library plus the (at most one) edit, applied as a single candidate.

---

## 9. Validation gate (primary protocol decision)

For a round, both the **current** and **candidate** libraries are evaluated on the **same eligible selection
PRs** (same seed, same nesting, same repair settings). PR-level outcomes are used.

```
gained    = # selection PRs failing with the current library, passing with the candidate
regressed = # selection PRs passing with the current library, failing with the candidate
net       = gained - regressed
accept    = (net >= gate_min_gain) and (regressed <= gate_max_regress)
```

Errored PRs (`passed = None`) are excluded from both counts and are never counted as regressions.

### 9.1 Calibrated threshold

The gate threshold is **calibrated from fresh-repeat selection noise**, not fixed in advance at the pilot
value:

1. Before evolution, evaluate the **unchanged initial library** (round 0 is empty, so `static --partition
   selection` is the exact evaluator) on the selection partition `K = 5` times with fresh evaluations
   (`--fresh`, no cache reuse, identical prompts, same seed, same PR set). This is a no-op (A/A) experiment:
   any gain or regression it shows is noise. The residual repair-agent and Docker variance is what the gate
   must clear; calibration at round 0 is conservative for later, larger libraries.
2. For each of the 5 repeats, compute `net = gained - regressed` against repeat 1.
3. Set `gate_min_gain = max(1, ceil(95th percentile of |net|))`. If every repeat is identical, use 1.
4. Set `gate_max_regress = 1` (the proposal's tolerance).
5. Record the five repeats, the percentile, and the resulting threshold in `results/FROZEN.json` (and the raw
   calibration file under `results/`), before any evolution round.

The calibrated values are passed to the final runs explicitly as `--gate-min-gain` / `--gate-max-regress`.
Calibration uses only development/selection data and never the test partition.

### 9.2 Sensitivity: the SkillOpt rule

The original SkillOpt-style rule (`gained >= 1` and `regressed <= 1`, i.e. `--gate-min-gain 1
--gate-max-regress 1`) is **reported as a sensitivity analysis** applied to the same recorded round data:
which rounds would it have accepted, and what would the resulting libraries have been? It is not the primary
gate unless calibration yields exactly 1. If the calibrated threshold exceeds 2, the gate is weak relative to
selection noise; report both the calibrated and SkillOpt results and discuss the power limitation.

---

## 10. Evolution loop and stopping rule

* Maximum **5** evaluated rounds per condition (`--rounds 5`).
* A round evaluates the current library on development, proposes at most one edit, evaluates candidate and
  current on selection (gated conditions), and accepts or rejects.
* The candidate replaces the current library only when accepted. A rejected round leaves the library
  unchanged.
* **Stop after 2 consecutive rejections** (`--stop-after-rejections 2`) or after 5 rounds, whichever comes
  first.
* An accepted round consumes one round; a rejected round also consumes one round.
* The `scoped` ungated ablation (only if time) applies every proposed edit and runs a fixed **3 rounds** (or
  the scoped gated condition's number of evaluated rounds if smaller), with no gate.
* All rounds use the same seeded dev/selection PR sets. Rounds for different conditions run sequentially so
  that selection baselines and image downloads can be reused where the frozen cache permits.
* Raw per-round records go to `results/evolution/<condition>.jsonl`; library snapshots to
  `results/skills/<condition>/round_<n>.json`.

---

## 11. Primary outcome and analysis set

**Primary metric.** Comment-level pass rate on the **locked test partition**, with errors counted as failed:

```
pass_rate = (# reference comments whose Stage-4 test passes after repair)
            / (# reference comments of all eligible test PRs)
```

Denominator is **70 comments** across **52 PRs** and **21 repositories**. The lambda to estimate is the
performance of a *frozen* library, not of a process; each condition's final library is scored once.

**Primary contrast.** `scoped` gated vs `global` gated, two-sided.

**Reference contrasts (secondary, not used to select the primary claim).** static; gated vs static;
`scoped` ungated if run.

Selection results are never reported as held-out performance. The gap between selection and test pass rate
is reported only as a transfer diagnostic.

---

## 12. Secondary metrics

1. PR-level all-tests-pass rate on test (errors failed);
2. pass rate excluding errors (complete-case sensitivity);
3. number of infrastructure errors, by condition and partition;
4. accepted edits, rejected edits, and rounds evaluated per condition;
5. regressions per accepted edit and net gain per accepted edit, from round records;
6. selection→test transfer gap (difference in comment-level pass rate);
7. token usage per PR evaluation, per repair, and per passing comment, from the token ledger;
8. wall-clock seconds per repair and per round, from the duration ledger;
9. retrieval diagnostics: skills per review, scope mix, budget saturation, cap-6 hits;
10. library size and scope distribution at freeze;
11. run-to-run flip rate: comments whose outcome differs between the frozen static run and a fresh repeat
    (if a repeat is affordable), plus the A/A calibration flips;
12. released-tool comparison on the same instances, reported as reference only (different agents and
    harness, not a controlled arm).

---

## 13. Statistics

All intervals and tests are computed at the comment level unless stated; PR-level versions are reported as
sensitivity.

* **Repository-clustered bootstrap**, 10,000 resamples: resample the 21 test repositories with replacement,
  take all comments of each sampled repository, and report the 95% percentile CI of the pass rate (and of
  paired differences for the primary contrast).
* **Paired permutation test**, 10,000 permutations, two-sided: sign-flip the paired per-PR (or per-repo)
  difference between conditions, and compare the observed mean difference to the permutation distribution.
  Pairing is by PR id; both conditions are scored on exactly the same 52 PRs.
* **McNemar test** on paired comment outcomes for each contrast (exact binomial for small discordant counts;
  otherwise the continuity-corrected chi-square). Report discordant-pair counts, not just p.
* Effect sizes: absolute pass-rate difference with CI; risk difference; number of PRs gained/lost.
* No multiplicity correction is applied across the secondary endpoints; those are labelled exploratory. The
  primary contrast is a single pre-registered test.
* If a condition errors on more than 10% of test PRs, report the errors-as-failed result as primary and the
  complete-case result alongside it, and state that the deviation makes the comparison less reliable.

---

## 14. Freeze and locked-test discipline

**Freeze happens before the test partition is touched.** The freeze record `results/FROZEN.json` is written,
committed, and must contain at least:

* UTC timestamp, frozen git commit, `code_manifest` (SHA-256 of `harness.py`, `evolve.py`, `reviewer.py`,
  `data.py`, `proxy_hooks.py`; a dirty tree is refused), `HARNESS_VERSION`, model id and endpoint;
* split seed and the exact development / selection / test PR id lists used (eligible sets);
* gate calibration record (5 repeats, observed nets, percentile, chosen `gate_min_gain`,
  `gate_max_regress`) and the exact flags for every final run;
* SHA-256 of each condition's final library (`results/skills/<condition>/round_<n>.json`), the round count,
  accepted/rejected history, and the retrieval configuration (400 tokens, newest-first, dedup, cap 6);
* the token/duration ledger summary at freeze;
* a statement that no test-partition evaluation has been run.

**Then, and only then**, unlock the test partition with `--unlock-test` and score each frozen library
**once** on the same 52 eligible test PRs. Rules:

* The test partition is guarded in code; `sample()`, `static`, `evolve` and `score` refuse it unless
  `--unlock-test` is given, and then require a matching `code_sha`, `code_manifest`, `harness_version`,
  `libraries` hashes and `test_ids` in `results/FROZEN.json`.
* Use `score --salt <name>` for the locked pass: the salt makes it a genuine new draw and, because it is
  deterministic, an interrupted pass resumes without re-rolling completed PRs. `--fresh` (a random salt) is
  not used for the locked pass. `--allow-overwrite` is forbidden.
* One pass per condition. `--resume` may be used only to complete PRs that never produced a result, with the
  identical frozen config, salt and flags; it may not re-roll completed PRs or change thresholds.
* After the first test result is visible, no skill edits, threshold changes, round extensions, or condition
  changes are allowed. If a rerun is unavoidable, it is logged as an amendment and both runs are reported.
* Test comments/PRs are never used for development, calibration, gating, or prompt design.

---

## 15. Ledger, budget, and resource limits

* Because ASU hosting is free, dollar prices are zero and dollar figures are **nominal only**. The
  authoritative ledger is **tokens and wall-clock duration** per stage (`review`, `repair`, `optimizer`),
  recorded in `runs/ledger.jsonl`.
* Each Claude run has a 1200-second timeout (`REPAIR_TIMEOUT`); a timed-out repair is an error (`passed = None`),
  excluded from the gate and failed in the primary metric. `pip install -e .` has a 300-second timeout
  (`PIP_TIMEOUT`); a timeout before the agent starts is an unbilled infra error and is retried once.
* Transient endpoint failures are retried twice with 60/120 s backoff before the run stops: rate limits (429),
  and a 403 `unrecognized_model` from the ASU routing layer. A genuinely invalid key (401/`invalid api key`)
  still stops the run at once. This was added after a transient 403 aborted the first clean baseline at 10/89 PRs.
* `SSE_CAP_USD` optionally sets a nominal spend cap (`CAP_USD` is uncapped by default because the model is
  free). It is a safety valve, not a cost measurement: a run stops cleanly when the next paid call would
  exceed the cap.
* Each PR's benchmark image is removed by exact tag as soon as its repair finishes (at most `SSE_WORKERS`
  images on disk). `SSE_KEEP_IMAGES=1` disables this for debugging. A preflight/pull guard refuses to run
  below `SSE_MIN_FREE_GB` (default 30 GB), because a full disk crashes Docker Desktop and silently turns
  into failed reviews.
* Content-addressed caching under `runs/cache/` is allowed for development and selection. `--fresh` bypasses
  it for calibration, final fresh baselines, and the locked test pass; `--resume` reuses completed work.

---

## 16. Known deviations from the proposal

| # | Deviation | Why | Consequence |
|---|---|---|---|
| D1 | The proposal assumed **Claude Sonnet 4.6 for repair**; the final runs use **`qwen36-27b` for reviewer, optimizer, and repair**. | ASU Voyager hosting is free and available; repair had to speak the Anthropic API through a local LiteLLM proxy. | Absolute scores are not comparable to the Sonnet pilot in `results/sonnet_4_6/` or to the released-tool numbers, which used their own agents. All conditions share the same model, so the internal comparison is valid; external comparability is explicitly limited. |
| D2 | **Oversize PRs (>120k diff characters) and one no-image PR are excluded**: dev 89, selection 34, test 52 (from 92/37/55). | The reviewer has a hard diff cap; the missing image cannot be evaluated. Exclusions use only inputs, never outcomes. | Reported denominators are 109 / 42 / 70 comments. Selection and test are slightly smaller than the proposal targets. |
| D3 | **`MAX_EDITS = 1`** per round, not 2. | With small selection sets, a 2-edit candidate makes the gate's gain/regression signal harder to attribute and doubles some evaluation cost. | Slower library growth; each accepted edit is cleaner to interpret. Net gain per accepted edit remains comparable across conditions. |
| D4 | The gate uses a **calibrated net-gain threshold** instead of the fixed SkillOpt rule; SkillOpt is reported as sensitivity. | The pilot showed that selection noise can swamp a fixed ±1 rule. | If calibration yields 1, the gates coincide. Both are reported. |
| D5 | **Four test-partition PRs have JavaScript tests**; they are retained and scored as released. | The harness and images are used unmodified; no JS-specific work was completed in time. | These PRs may fail for environment reasons; errors count as failed in the primary metric, and complete-case results are reported as sensitivity. |
| D6 | **Code Review Bench (CRB) as an external test set was dropped.** | Time and budget were spent on stabilising the c-CRAB harness and the evolution loop; CRB would add a second, differently-scored pipeline. | External validity rests on the repository-disjoint test partition only. Stated as a limitation. |
| D10 | The first clean baseline (2026-11-24) was aborted after 10/89 development PRs by a transient ASU 403 `unrecognized_model`; a direct endpoint check immediately afterwards succeeded. | The repair path now retries a 403 `unrecognized_model` twice with backoff before stopping, like a rate limit. The completed 10 PRs are reused under the same salt; the run resumes. | Only successful draws are reused; the retry changes how failures are handled, not how a completed review/repair is scored. The run is reported as one baseline with the infrastructure event noted. |
| D9 | The first overnight static baseline (2026-11-23/24) filled the Docker disk (image retention was bounded only at run end); 21/89 development and 33/34 selection repairs failed, mostly from Docker/ENOSPC. | The harness now removes each image after its repair and refuses to start below 30 GB free. The affected baselines were re-run; the invalid selection run is not used. | Infrastructure failures are counted as failed in the primary metric and excluded from the gate; the re-run and complete-case results are reported. This is exactly the failure mode the harness now guards against. |
| D7 | The **pilot used sampling prefixes (dev 12, selection 8)** and dollar costs; final runs use full eligible partitions and a **token/duration ledger**. | The pilot was a feasibility check, not a result. | Pilot outputs are archived in `results/qwen_pilot/` and are not final results. |
| D8 | Cached dollar figures are nominal (prices are 0). | ASU hosting is free; token and duration records are the real resource ledger. | No dollar-based conclusions; compute is reported in tokens and seconds. |

---

## 17. Amendments

Append-only. Each amendment records:

```
### A<k> — YYYY-MM-DD (before/after freeze)
Decision changed:
Reason:
Evidence used (must not be test outcomes unless after freeze, in which case both results are reported):
```

No amendments have been made at pre-registration time (2026-11-23).

---

## 18. Approximate timeline

| Milestone | Target |
|---|---|
| Phase A: harden + offline tests + pilot + full static baseline (dev 89 + selection 34) | 2026-11-23 to 2026-11-25 |
| Gate calibration (5 fresh A/A repeats on selection) | 2026-11-25 |
| Phase B: global gated evolution (≤5 rounds) | 2026-11-25 to 2026-11-28 |
| Phase B: scoped gated evolution (≤5 rounds) | 2026-11-27 to 2026-11-29 |
| Scoped ungated ablation, if time | 2026-11-29 |
| Phase C: freeze `results/FROZEN.json` + locked test once + statistics | 2026-11-29 to 2026-11-30 |
| Phase D: figures + 5-min presentation | 2026-11-30 to 2026-12-02 |
| Phase E: final 8-page report | 2026-12-02 to 2026-12-04 |
