# Progress Report: Test-Guided, Scope-Aware Skill Evolution for Code Review Agents
Group 22 (CSE 572). Draft. Every number below comes from `results/` (`summary.json`, `census.json`, `splits.json`, `released_tools.csv`) and `runs/ledger.jsonl`.

> **Status in one paragraph.** The full pipeline now runs end to end on real c-CRAB pull requests: data preparation, a skill-aware reviewer, the Docker repair-and-test harness, the skill library with a sanitizer, optimizer and validation gate, and the statistics code. We ran a static baseline and one gated evolution round per condition on small samples (12 dev PRs, 8 selection PRs) for $10.69 in API spend. Both conditions' candidate skill edits were rejected by the gate, so **we have no evidence yet for or against the hypothesis**. These runs show the system works and give us measured costs. They are not a test of the research question.

## 1. Progress against the proposal

| Planned item | Status |
|---|---|
| c-CRAB pinned at `856dfa3`, census reproduces the funnel | Done. 671 → 410 → 339 → 184 PRs, 234 comments, 67 repos (`census.json`) |
| PR types from metadata | Done. 67 bug fix / 68 feature / 49 other (`metadata.problem_domain`) |
| Repository-disjoint splits | Done. Seed 2685 hits all targets exactly (see §2) |
| Review agent with skill retrieval, ≤5 findings | Done (`reviewer.py`, `claude-sonnet-4-6`, temperature 0) |
| c-CRAB repair + test harness | Done and run end to end (`harness.py`, wraps the c-CRAB flow) |
| Skill library, sanitizer, optimizer (≤2 edits), gate | Done (`evolve.py`), offline-tested |
| Static baseline | Run on 12 dev PRs only |
| Global vs scoped gated evolution | One round each on 12 dev / 8 selection PRs |
| Released tools rescored on our partitions | Done. Published rates reproduced exactly |
| Bootstrap CI, permutation test | Implemented and tested. Only the CI is used so far |
| Scoped-without-gating ablation | Flag exists (`--no-gate`), not run |
| Locked test set, 3 repeated runs, human labeling (κ), Code Review Bench | **Not started.** The system is blocked from the test partition in code |

## 2. Data
- **Census** (all from the released files): stage 0 671 PRs / 1313 comments, stage 1 410 / 595, stage 2 410 / 595, stage 3 339 / 485, stage 4 184 / 234.
- **Splits.** Greedy seeded assignment of whole repositories, using only sizes and PR types, never outcomes.

| Partition | PRs | Comments | Repos | Bug fix | Feature | Other |
|---|---|---|---|---|---|---|
| Development | 92 | 112 | 25 | 34 | 35 | 23 |
| Selection | 37 | 47 | 20 | 14 | 7 | 16 |
| Test (locked) | 55 | 75 | 22 | 19 | 26 | 10 |

- **Released tools on our partitions** (comment pass rate; reproduces the published 32.1 / 24.8 / 23.1 / 20.1 / 41.5% on all 234):

| Partition | Claude Code | Devin | PR-Agent | Codex | All four |
|---|---|---|---|---|---|
| Development (112) | 25.9% | 25.0% | 20.5% | 10.7% | 37.5% |
| Selection (47) | 42.6% | 29.8% | 29.8% | 36.2% | 48.9% |
| Test (75) | 34.7% | 21.3% | 22.7% | 24.0% | 42.7% |

- **Things we found that differ from the proposal:**
  - 4 of the 184 PRs have JavaScript tests (3 ccxt, 1 zulip), not Python. All four are in the test partition.
  - Selection has only 7 feature PRs, so any per-type selection analysis is thin.
  - The proposal says 2 PRs lack a prebuilt image. The image report lists `python__cpython-104029` (in our selection set) and `python__cpython-30745` (not in the 184) as skipped, and we exclude the former.
  - 8 PRs have diffs over 120k characters, with the largest around 710k. We exclude those from sampling for all conditions (3 in dev, 2 in selection, and 3 in test, which will need a decision) because they overflow the repair agent's context. This is a deviation and it biases samples toward smaller PRs.

## 3. What we built
- **Reviewer.** Pinned Claude Sonnet 4.6 via LiteLLM. Input is the PR title, body, diff and retrieved skills, and output is validated JSON of at most 5 findings. No gold data is reachable.
- **Harness.** Reuses the c-CRAB flow (container reset, the vendor's repair prompt, per-test verification). Differences from the vendor: it uses an API key rather than a credentials file, runs only the Stage 4 tests, and caps each repair at $1.50. It scores a PR as passing only if all its tests pass.
- **Skill library.** Skills are tagged global, bug_fix, feature or other. The scoped condition retrieves matching-type skills first, then global ones, and the global condition draws from one pool. Both use the same 400-token budget.
- **Sanitizer.** Built as an allowlist. The optimizer sees only PR type, the reviewer's own finding text with paths and file names stripped, and the count of tests passed. A check on the exact payload fails on any test source, gold comment text, repo name, instance id or file path.
- **Gate.** Accept a candidate iff at least 1 selection PR is gained and at most 1 regresses. PRs with infrastructure errors are excluded from the comparison.
- **Safeguards.** A spend ledger with a hard cap of $22 checked before every paid call, content-addressed caching so repeated evaluations are free, automatic deletion of benchmark Docker images after use, and secret scrubbing of everything written to disk.
- **Verification.** Offline tests (`test_core.py`) cover the census, splits, gate, sanitizer, retrieval, ledger, caching, error handling, image cleanup and statistics. Key behaviors were mutation-checked (break the logic, confirm a test fails). The data code and the harness (reviewed twice, after fixes) were independently reviewed before any money was spent. The last two small commits (narrowing the fatal-error check, image cleanup) have offline tests but no independent review.

## 4. Preliminary results (small, single run, not evidence for the hypothesis)

**Static reviewer, 12 development PRs (16 comments).** 4 of 16 comments pass, **25.0%**, with a repository-clustered bootstrap 95% CI of 4.8% to 42.9%. One PR hit the 600-second repair timeout and counts as failed (26.7% if excluded). The figure compares this to the released tools on the same 16 comments (`fig_static_vs_released.png`):

| Reviewer | Passed (of 16) |
|---|---|
| Ours, static | 4 |
| Claude Code / Devin / PR-Agent | 4 / 4 / 4 |
| Codex | 2 |
| All four combined | 4 |

Our static reviewer is indistinguishable from the released tools on this sample. With 16 comments, that says almost nothing about which is better.

**Evolution, one round each (12 dev PRs for feedback, 8 selection PRs for the gate).**

| Condition | Edits proposed | Selection pass rate, baseline → candidate | Gained / regressed | Decision |
|---|---|---|---|---|
| Global | 2 (both global) | 5/9 → 4/9 comments | 0 / 1 | Rejected |
| Scoped | 2 (1 global, 1 bug_fix) | 5/9 → 4/9 comments | 0 / 1 | Rejected |

Both libraries therefore remain empty and the two conditions are identical in outcome. One regressed PR out of eight is within run-to-run noise, so we cannot say whether the edits hurt. A second round would not help: the optimizer is deterministic and, with an unchanged library, would propose the same edits and hit the cache.

**Observations worth reporting.**
- The proposed edits are generic checklist items (duplicate assignments, identical if/else branches, parameter validation). One scoped edit contains a code snippet (`self.need_unique_check = True`) copied from the reviewer's own finding on a pandas dev PR. The sanitizer strips paths and file names but not code identifiers, so the optimizer can overfit to a single PR. This is not gold-data leakage (the text is the reviewer's own output), but it is a risk for generalization and we will tighten it.
- The gate is a high bar at this scale: with 8 selection PRs, one regression blocks an edit that gains nothing.

## 5. Cost (from `runs/ledger.jsonl`)
- **Total spend: $10.69** (37 repairs, 37 reviews, 2 optimizer calls), against a self-imposed $25 limit and a $22 code cap.
- **Mean cost per PR evaluation: $0.29** (review about $0.03, repair about $0.26). The $1.50 timeout is counted at its maximum because the true cost is unknown. Oversize PRs are excluded and would cost more.
- **Time.** About 20 minutes for 12 PRs on an arm64 laptop, because the c-CRAB images are amd64-only and run under emulation, with 2 parallel workers.
- **Projection for the full proposal (rough worst-case estimate).** Assumptions: $0.29 per PR evaluation, **5 evolution rounds (our assumption; the proposal does not fix a number)**, and no cache savings beyond reusing the previous round's selection baseline.

| Item | Evaluations | Cost |
|---|---|---|
| Global and scoped gated evolution (round 1 is 92 dev + 37 baseline + 37 candidate; later rounds at most 92 + 37; shared first-round work counted once) | 1,235 | about $358 |
| Scoped no-gate ablation (dev only, 5 rounds) | 460 | about $133 |
| Locked test (55 PRs × 4 conditions × 3 repeats) | 660 | about $191 |
| **Total** | **2,355** | **about $680** (about $550 without the ablation) |

  It would be lower if the gate rejects most rounds, because rejected rounds with an unchanged library reuse the cache. It is far above a $25 budget, so we need to cut scope or get credits (see §7).

## 6. Deviations and risks
- Today's runs use 12 dev and 8 selection PRs. The proposal's samples are the full partitions.
- One evolution round, not multiple, and no repeated runs, so no flip-rate estimate.
- The gate on 8 selection PRs is much noisier than the gate on 37.
- Our repair agent runs under amd64 emulation, so wall-clock times are not representative.
- The agent inside the container has the repo at the head commit and can in principle see future git history. This is identical to the c-CRAB baselines and so does not affect comparisons, but should be disclosed.
- The test partition has only 75 comments across 22 repositories, so confidence intervals will be wide. A real difference between scoped and global may not be detectable.
- 4 JavaScript PRs in the test partition use a different test runner.
- This code was developed with Claude Code (AI-assisted). The team should decide how to describe individual contributions for the report.

## 7. Remaining work and owners
| Owner | Next steps |
|---|---|
| Pradnya | Confirm image availability and JS test setup for the test partition. Decide how to treat the oversize PRs |
| Sachin | Static baseline on the full development and selection partitions. Run-to-run variance (flip rate) |
| Aman | Tighten the sanitizer to remove code identifiers. Run multi-round evolution with the scoped-without-gating ablation. Review the skill-retrieval ordering rule |
| Rithwik | Apply bootstrap CI and permutation test to the main comparison. Human labeling with Cohen's κ. Cost and token reporting |
| All | Decide scope and budget (§5). Freeze the system, then score the locked test set once, then Code Review Bench once |

**Decision needed from the team:** the full experiment is not affordable on a $25 budget. Options: (a) request more API credit, (b) cut to 2 rounds and 1 repeat run, (c) evolve on a subsample of dev and report that clearly.
