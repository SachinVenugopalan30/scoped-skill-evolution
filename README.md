# Scoped Skill Evolution for Code Review Agents

CSE 572 (Data Mining), Group 22. Does organizing evolved review skills into **global + PR-type-scoped tiers**
improve held-out pass rate on [c-CRAB](https://github.com/c-CRAB-Benchmark/dataset) compared with a single
global skill library, when both use the same validation gate?

How it works: an LLM reviewer reads a PR diff plus retrieved skills and returns up to 5 findings. Claude Code
applies the findings inside the PR's Docker image, and c-CRAB's executable tests say pass or fail. An
optimizer sees sanitized pass/fail feedback and proposes at most 1 skill edit per round. A candidate library
is accepted only if it clears a calibrated net-gain gate on the held-out selection partition.

**Model:** `qwen36-27b` for **all three roles** (reviewer, optimizer, repair agent), hosted by ASU at
`https://openai.rc.asu.edu/v1`. Claude Code only speaks the Anthropic API, so the harness starts a local
LiteLLM proxy that translates to the ASU endpoint (see "How the model is wired").

**Status: final-experiment phase.** The pipeline is stable and the final protocol is pre-registered in
[`PROTOCOL.md`](PROTOCOL.md). Final runs use the full eligible partitions (development 89 PRs, selection 34,
locked test 52). Earlier small-sample outputs are archived: `results/qwen_pilot/` holds the partial qwen
pilot (12 dev / 8 selection, one round per condition), and `results/sonnet_4_6/` holds the older Sonnet 4.6
run. **Neither is a final result.** The test partition is guarded and must only be run once, after the freeze
record `results/FROZEN.json` exists.

What changed since the progress report:

* all roles now run on `qwen36-27b` (the proposal assumed Sonnet 4.6 for repair);
* final runs use the full eligible partitions, not sampling prefixes;
* the gate is calibrated from fresh-repeat selection noise (`--gate-min-gain`), with the SkillOpt rule
  reported as a sensitivity;
* the ledger records tokens and wall-clock duration, not meaningful dollars (ASU hosting is free);
* one edit per round (`MAX_EDITS = 1`);
* the locked test partition requires an explicit `--unlock-test` and is scored exactly once.

## Requirements
- Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/)
- Docker. The c-CRAB images are amd64-only, so on Apple Silicon they run under emulation (slow and
  CPU-heavy; see Tips)
- git
- An ASU Voyager API key. If the endpoint is unreachable, try toggling the ASU VPN; it worked here with the
  VPN off
- Docker Desktop (the containers reach the proxy at `host.docker.internal`)
- Free disk: benchmark images are 2 to 4 GB each (some 5.5 GB). Each PR's image is removed as soon as its
  repair finishes, so a run holds at most `WORKERS` images; a free-space guard refuses to start below 30 GB.

## Setup
```bash
git clone --recurse-submodules https://github.com/SachinVenugopalan30/scoped-skill-evolution.git
cd scoped-skill-evolution
uv sync
```
If you cloned without `--recurse-submodules`: `git submodule update --init`.

Create a `.env` file **in this folder** (it is gitignored; never commit it):
```
VOYAGER_API_KEY=your-key-here
```

## Run it, start to finish
Commands below use `uv run`. Steps 1 to 3 cost nothing. The run order matters: development and selection
only until the freeze, then the locked test pass.

1. **Data census and splits** (writes `results/census.json`, `results/splits.json`):
   ```bash
   uv run python data.py
   ```
   Reproduces the c-CRAB funnel (671 → 184 PRs, 234 comments, 67 repos) and the seeded repository-disjoint
   dev / selection / test split.
2. **Released-tool baselines** (writes `results/released_tools.{json,csv}`):
   ```bash
   uv run python analyze.py
   ```
   Rescores PR-Agent, Devin, Claude Code and Codex from the c-CRAB release; should reproduce
   32.1 / 24.8 / 23.1 / 20.1 / 41.5%.
3. **Offline tests** (no network, Docker or spend):
   ```bash
   uv run python test_core.py
   ```
4. **Pilot: one PR end to end** (pulls one image):
   ```bash
   uv run python harness.py pilot --instance 'ManimCommunity__manim-509@1cbc24f'
   ```
   Prints per-test results, token usage and the running totals.
5. **Static baseline** (no learned skills) on the full eligible development partition:
   ```bash
   uv run python evolve.py static --partition development --n 89 --seed 0
   ```
   Writes `results/static_development.json`.
6. **Calibrate the gate** (PROTOCOL §9.1): evaluate the unchanged baseline library on the selection partition
   5 times with fresh evaluations, and record the 95th percentile of the observed net gain as the gate
   threshold. Use a distinct deterministic salt per repeat and save each output, e.g.:
   ```bash
   uv run python evolve.py static --partition selection --n 34 --seed 0 --salt calib-1 --allow-overwrite
   uv run python evolve.py static --partition selection --n 34 --seed 0 --salt calib-2 --allow-overwrite
   # ... calib-3 .. calib-5
   ```
   Write the chosen threshold, the five repeats and the resulting files into `results/FROZEN.json` before
   evolving. Each run writes `results/static_selection.json`, so copy it aside after each repeat.
7. **Gated evolution, one condition at a time** (substitute the calibrated `<tau>`):
   ```bash
   uv run python evolve.py evolve --condition global --dev-n 89 --sel-n 34 --rounds 5 \
     --gate-min-gain <tau> --gate-max-regress 1 --stop-after-rejections 2 --seed 0
   uv run python evolve.py evolve --condition scoped --dev-n 89 --sel-n 34 --rounds 5 \
     --gate-min-gain <tau> --gate-max-regress 1 --stop-after-rejections 2 --seed 0
   ```
   Add `--no-gate` for the ungated ablation (fixed 3 rounds):
   ```bash
   uv run python evolve.py evolve --condition scoped --dev-n 89 --sel-n 34 --rounds 3 --no-gate --seed 0
   ```
   Run the conditions **one after the other** so later conditions reuse the cached baseline where valid.
   Outputs: `results/evolution/<condition>.jsonl`, `results/skills/<condition>/round_<n>.json`.
8. **Summary numbers and figure** (writes `results/summary.json`, `results/fig_static_vs_released.png`):
   ```bash
   uv run python report_numbers.py
   ```
9. **Freeze, then score test once.** Commit `results/FROZEN.json` with the frozen libraries, gate threshold,
   flags, seeds, eligible id list and code manifest (PROTOCOL §14). Only then score each frozen library
   exactly once (the file is written once; `--allow-overwrite` is forbidden for the real pass):
   ```bash
   uv run python evolve.py score --partition test --condition static --n 52 --unlock-test --salt test-static
   uv run python evolve.py score --partition test --condition global \
     --library results/skills/global/round_<n>.json --n 52 --unlock-test --salt test-global
   uv run python evolve.py score --partition test --condition scoped \
     --library results/skills/scoped/round_<n>.json --n 52 --unlock-test --salt test-scoped
   ```
   `--salt` is required for the test pass: it records the draw so an interrupted run can be resumed without
   re-rolling completed PRs. Do not tune, re-gate or extend rounds after this point.

Use the same `--dev-n`, `--sel-n` and `--seed` across runs so cached results are reused. `--salt <name>`
selects a deterministic fresh draw (same salt + prompt = same cached result), so a long run can resume without
re-rolling. `--fresh` is the shorthand for an unrecorded random salt; it is mutually exclusive with `--resume`.

## Flags
| Flag | Applies to | Meaning |
|---|---|---|
| `--partition {development,selection,test}` | `static`, `score` | Which split to evaluate. `test` additionally requires `--unlock-test`. |
| `--n` | `static`, `score` | Number of eligible PRs (seeded, nested, proportional by PR type). Omit on `score` for all eligible. |
| `--condition {static,global,scoped}` | `score` | Library whose frozen snapshot is being scored. |
| `--library PATH` | `score` | Snapshot to score; default is the latest `results/skills/<condition>/round_<n>.json`. |
| `--condition {global,scoped}` | `evolve` | Library organization. |
| `--dev-n`, `--sel-n` | `evolve` | Eligible development / selection PR counts (seeded and nested). |
| `--rounds` | `evolve` | Maximum evaluated evolution rounds (protocol cap: 5; ungated ablation: 3). |
| `--no-gate` | `evolve` | Apply every proposed edit instead of running the selection gate. |
| `--gate-min-gain` | `evolve` | Required **net** gain `gained - regressed` on selection. Set from calibration; `1` reproduces the SkillOpt rule only when there are no regressions. |
| `--gate-max-regress` | `evolve` | Maximum selection regressions tolerated (default `1`). |
| `--stop-after-rejections` | `evolve` | Stop after this many consecutive rejected rounds (default `2`). |
| `--fresh` | `static`, `evolve`, `score` | Force one new evaluation under a random salt. Mutually exclusive with `--resume`. For reproducible runs use `--salt`. |
| `--salt NAME` | `static`, `evolve`, `score` | Deterministic fresh-draw key: same salt + prompt reuses the stored draw. Required for the locked test pass. |
| `--resume` | `static`, `evolve` | Reuse completed results and continue; never re-rolls completed PRs. |
| `--unlock-test` | `static`, `evolve`, `score` | Allow the locked test partition. Only after `results/FROZEN.json` (code + manifest + library hashes + test ids); one pass only. |
| `--allow-overwrite` | `static`, `score` | Deliberately replace an existing scored output; forbidden for the real test pass. |
| `--seed` | `static`, `evolve`, `score` | Seed for nested, type-proportional sampling (default `0`). |

## Environment variables
| Variable | Meaning |
|---|---|
| `VOYAGER_API_KEY` | ASU Voyager key. May live in `.env` or the environment; never commit it. |
| `SSE_CAP_USD` | Sets a nominal spend cap for a run. Unset by default (uncapped), because ASU hosting is free; since prices are 0 this is a safety valve, not a cost measurement. |
| `SSE_KEEP_IMAGES` | Set to `1` to keep benchmark Docker images after a run for debugging. Default is deletion by exact tag immediately after each repair; each image is 2 to 4 GB. |
| `SSE_MIN_FREE_GB` | Minimum host free space before a run and before each pull (default `30`). A full disk crashes Docker Desktop and turns into failed reviews; this guard stops cleanly instead. |
| `SSE_WORKERS` | Parallel Docker containers (default `2`; `3` is safe on a 24 GB host, `4` is not). |

## How the model is wired
- `reviewer.py` and the optimizer call `openai/qwen36-27b` through LiteLLM with thinking disabled
  (`chat_template_kwargs`), since this is a reasoning model that otherwise spends its token budget on
  thinking and returns empty content.
- The repair agent is Claude Code inside the container. `harness.start_proxy()` (called by the CLIs) starts
  a LiteLLM proxy on `127.0.0.1:4000` that exposes the Anthropic API and forwards to ASU, and stops it at
  exit. Config is generated at `runs/proxy.yaml`.
- The proxy must use the `hosted_vllm/` provider: with `openai/` the streamed tool-call arguments are dropped
  and the agent loops on empty tool calls.
- `proxy_hooks.py` merges Claude Code's multiple system messages into one, because Qwen's chat template
  accepts only a single system message.
- The ASU key stays on the host. Containers get only a random per-run token that the local proxy accepts.

## Tokens, duration, budget and safety
- ASU hosting is free for us, so `PRICE_IN` and `PRICE_OUT` in `reviewer.py` are 0 and any dollar column is
  **nominal**. The authoritative ledger is **tokens and wall-clock duration** per stage in
  `runs/ledger.jsonl` (`review`, `repair`, `optimizer`), alongside the instance id and timestamp.
- Measured on `qwen36-27b`: a pilot repair used about 13k input and 0.3k output tokens; repairs on the
  emulated amd64 images took a median of about 4-6 minutes each. Each Claude run has a 1200-second timeout and
  `pip install -e .` gets 300 seconds. Use these for planning, not dollar figures.
- There is no dollar cap by default (`CAP_USD` is infinite) because the model is free. Set `SSE_CAP_USD` to
  bound a run; a run then stops cleanly when the next call would exceed it. The ledger is shared across
  models, so old entries count toward the cap; move the file aside to reset it. Nominal dollars use
  `PRICE_*` and are 0 here.
- Results are cached under `runs/cache/` by content, so re-running the same step is free. `--resume`
  continues an interrupted run; `--fresh` forces a genuine new evaluation.
- **Benchmark images:** 2 to 4 GB each. The harness pulls one image per PR and deletes exactly the two tags
  it created for that instance as soon as the repair finishes, so at most `SSE_WORKERS` images exist at once.
  It never runs `docker prune`, never uses `-f` or wildcards, and touches nothing else on the machine. A
  preflight free-space check (`SSE_MIN_FREE_GB`, default 30 GB) refuses to start on a nearly full disk.
  `SSE_KEEP_IMAGES=1` disables cleanup (only useful when debugging; expect many GB).
- The key is scrubbed from everything written under `runs/` and `results/`.

## Guarded test partition
The locked **test partition is blocked in code**: `sample()`, `pilot` and scoring refuse it unless
`--unlock-test` is passed. Use it only after `results/FROZEN.json` is committed, score each frozen library
exactly once, and do not modify skills, thresholds or rounds afterwards. See `PROTOCOL.md` §14.

## Statistics and outputs
The primary metric is the comment-level pass rate on the locked test partition with errors counted as failed
(70 comments, 52 PRs, 21 repositories). Reported with it: repository-clustered bootstrap CIs (10k), paired
permutation tests (10k), McNemar tests on paired comment outcomes, PR-level all-pass rates, errors, accepted
edits, regressions per accepted edit, selection→test transfer gap, tokens and seconds per evaluation, and
retrieval diagnostics. `report_numbers.py` writes `results/summary.json` and the figure; the full analysis
plan is in `PROTOCOL.md`.

Archived outputs:
* `results/qwen_pilot/` — partial qwen pilot (static 12 dev, one evolution round per condition). Feasibility
  only; not final.
* `results/sonnet_4_6/` — older Sonnet 4.6 run, kept for comparison; not re-run on qwen.

## Tips
- **Docker CPU load:** the amd64 images run under emulation. In Docker Desktop, limit CPUs
  (Settings → Resources) and try "Use Rosetta for x86_64/amd64 emulation" (Settings → General).
- **Disk:** if Docker runs low, `docker images | grep -E 'reviewbench|c-crab-benchmark'` shows any leftover
  benchmark images; remove them by exact tag. `df -h` and `docker system df` show host and Docker usage. The
  harness now removes each image immediately and refuses to start below 30 GB free.
- **ASU 403 `unrecognized_model`:** transient routing error. The harness retries it twice with 60/120 s
  backoff before stopping. If it persists, check `GET https://openai.rc.asu.edu/v1/models` for the current
  model ids; a one-line completion with `litellm` distinguishes a real key/auth problem from a routing blip.

## Layout
| File | Purpose |
|---|---|
| `PROTOCOL.md` | Pre-registered final protocol: splits, exclusions, retrieval, sanitizer, gate, rounds, metrics, statistics, freeze rules, deviations |
| `data.py` | Census, PR types, repository-disjoint splits |
| `reviewer.py` | LLM reviewer (findings JSON) |
| `harness.py` | Docker repair-and-test runner, token/duration ledger, cache, image cleanup, `pilot` |
| `evolve.py` | Skill library, retrieval, sanitizer, optimizer, gate, round loop, `static` and `evolve` CLIs |
| `analyze.py` | Released-tool scoring, bootstrap CI, permutation test |
| `report_numbers.py` | Summary numbers and figure for the report |
| `proxy_hooks.py` | LiteLLM proxy hook: merges system messages for Qwen |
| `test_core.py` | Offline tests |
| `results/` | Committed outputs, archives (`qwen_pilot/`, `sonnet_4_6/`) and, after freeze, `FROZEN.json` |
| `runs/` | Local only: ledger, cache, raw logs (gitignored) |
| `vendor/c-crab/` | c-CRAB dataset and harness (submodule pinned at `856dfa3`, do not edit) |
