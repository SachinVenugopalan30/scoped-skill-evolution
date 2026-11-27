# Richer feedback: scope for team discussion (2026-11-26)

## Problem

Today the optimizer sees, for each development PR, the reviewer's own findings and `passed / total`. It learns from
**what the reviewer said**, never from **what it missed**. The gold comment is removed to prevent leakage, so the
optimizer can only guess generic advice ("prioritize correctness", "check imports"). Phase B shows it: 4 candidate
edits had nets −4, −3, −3 and +1, all within the A/A noise band.

## Proposal: tell the optimizer what kind of concern was missed, not what it was

For each **development** comment, add three low-bandwidth fields. Never selection or test.

| Field | Values | Source |
|---|---|---|
| `category` | one of 8: correctness/logic, error handling, input validation, API/contract misuse, tests/coverage, performance, concurrency/resources, maintainability/style | gold comment text, classified **once offline** |
| `resolved` | true/false | per-comment test outcome (already computed) |
| `located` | true/false: did any reviewer finding touch the same file as the gold comment | gold `path` vs finding `file`; the optimizer sees only the boolean |

Plus an aggregate over all development comments: resolved and missed counts per (PR type, category). Example:
"feature PRs: 2 of 14 test-coverage concerns resolved".

The optimizer still never sees gold wording, file paths, line numbers, diffs, test code or repository names.
`assert_clean` is unchanged and still checks the exact payload.

**Why this could help the research question.** Categories per PR type give the optimizer a reason to write
PR-type-scoped skills (e.g. "feature PRs: check tests cover the new path"). So far scoped has proposed only global
skills, so scoping has not actually been exercised.

## Leakage argument

* An 8-way label plus two booleans per comment, on development PRs only. Selection and test gold are never read.
* Splits are repository-disjoint, so a category learned on development repositories says nothing PR-specific about
  test repositories.
* Main risk: the optimizer writes skills that fit development (for example, if 40% of misses are tests/coverage).
  That is intended learning, and the gate on selection plus the locked test measure whether it transfers.

## Work

| Step | Effort |
|---|---|
| `classify_gold.py`: the same model labels 107 development comments into the 8 categories at temperature 0, saved to `results/gold_categories_dev.json` | ~1 h code, minutes to run |
| Team spot-check: two people label 30 comments blind and report Cohen's κ against the model (the proposal already promised a κ labelling) | ~1 h of people time |
| `build_feedback`: add `category`, `resolved` and `located` to examples, plus the aggregate table, behind a `--feedback rich` flag | ~1–2 h, plus offline tests |
| Amendment A4: the new arms are labelled exploratory; the current arms remain primary | 30 min |
| Phase B for `global-rich` and `scoped-rich` on `pcloud` (5 rounds max, stop after 2 rejections) | ~7–18 h per condition, ~1–1.5 days for both |
| Test pass for the two extra libraries (52 PRs each) | ~2–3 h |

## Timeline risk

The freeze target is 11-29. If the team decides by the morning of 11-27: code and classification on 11-27, Phase B
from 11-27 to 11-28/29, freeze and test on 11-29/30. Tight but feasible, with little slack for another bug.

## What it does not fix

The **gate noise**. Eight of 34 selection PRs flip between identical runs, so a real effect of +1 or +2 is still
indistinguishable from luck. A partial mitigation, if there is time: re-evaluate only *accepted* candidates once
more and require the net to hold. That costs about 1 extra selection pass (~1 h) per acceptance.

## Options for the team

1. **Report as is (recommended if time is short).** An honest negative result: test-guided skill evolution with
   sanitized pass/fail feedback does not learn on c-CRAB, with measured evidence for both bottlenecks. Rich
   feedback becomes the concrete Future Work.
2. **Add rich feedback as two exploratory arms.** This answers "does knowing *what was missed* help?" and gives
   scoping a real chance. It costs about 2 days of compute and some schedule risk.
