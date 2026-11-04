# Scoped Skill Evolution for Code Review Agents

CSE 572 (Data Mining), Group 22. Does organizing evolved review skills into **global + PR-type-scoped tiers** improve held-out pass rate on [c-CRAB](https://github.com/c-CRAB-Benchmark/dataset) compared with a single global skill library, when both use the same validation gate?

How it works: an LLM reviewer reads a PR diff plus retrieved skills and returns up to 5 findings. Claude Code applies the findings inside the PR's Docker image, and c-CRAB's executable tests say pass or fail. An optimizer sees sanitized pass/fail feedback and proposes up to 2 skill edits per round. An edit is accepted only if it gains at least 1 selection PR and regresses at most 1.

**Status:** progress-report stage. The pipeline runs end to end; the experiments so far are small. See `results/progress_report_draft.md`.

## Requirements
- Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/)
- Docker. The c-CRAB images are amd64-only, so on Apple Silicon they run under emulation (slow and CPU-heavy; see Tips)
- git
- An Anthropic API key (the reviewer, optimizer and repair agent all use `claude-sonnet-4-6`)
- Free disk: images are 2 to 4 GB each. They are downloaded per PR and deleted after use

## Setup
```bash
git clone --recurse-submodules https://github.com/SachinVenugopalan30/scoped-skill-evolution.git
cd scoped-skill-evolution
uv sync
```
If you cloned without `--recurse-submodules`: `git submodule update --init`.

Create a `.env` file **in this folder** (it is gitignored; never commit it):
```
ANTHROPIC_API_KEY=your-key-here
```
Tip: set a monthly spend limit on the key in the Anthropic Console.

## Run it, start to finish
Commands below use `uv run`. Steps 1 to 3 cost nothing.

1. **Data census and splits** (writes `results/census.json`, `results/splits.json`):
   ```bash
   uv run python data.py
   ```
   Reproduces the c-CRAB funnel (671 → 184 PRs, 234 comments, 67 repos) and the seeded repository-disjoint dev / selection / test split.
2. **Released-tool baselines** (writes `results/released_tools.{json,csv}`):
   ```bash
   uv run python analyze.py
   ```
   Rescores PR-Agent, Devin, Claude Code and Codex from the c-CRAB release; should reproduce 32.1 / 24.8 / 23.1 / 20.1 / 41.5%.
3. **Offline tests** (no network, Docker or spend):
   ```bash
   uv run python test_core.py
   ```
4. **Pilot: one PR end to end** (about $0.12; pulls one image):
   ```bash
   uv run python harness.py pilot --instance 'ManimCommunity__manim-509@1cbc24f'
   ```
   Prints review cost, repair cost, per-test results and the running total.
5. **Static baseline** (no learned skills) on a seeded sample of dev PRs (about $0.30 per PR):
   ```bash
   uv run python evolve.py static --partition development --n 12
   ```
   Writes `results/static_development.json`.
6. **Gated evolution, one condition at a time:**
   ```bash
   uv run python evolve.py evolve --condition global --dev-n 12 --sel-n 8 --rounds 1
   uv run python evolve.py evolve --condition scoped --dev-n 12 --sel-n 8 --rounds 1
   ```
   Add `--no-gate` for the ungated ablation. Run the two conditions **one after the other** so the second reuses the cached baseline. Outputs go to `results/evolution/<condition>.jsonl` and `results/skills/<condition>/round_<n>.json`.
7. **Summary numbers and figure** (writes `results/summary.json`, `results/fig_static_vs_released.png`):
   ```bash
   uv run python report_numbers.py
   ```

Use the same `--n` and seeds across runs so cached results are reused.

## Cost and safety
- Every paid call checks a spend ledger (`runs/ledger.jsonl`) first. The hard cap is `CAP_USD = 22.0` in `harness.py`, and a run stops cleanly when it would exceed it.
- Results are cached under `runs/cache/` by content, so re-running the same step is free. A run interrupted by Ctrl-C or the cap can be started again.
- Measured cost is about $0.29 per PR evaluation. The full proposal is roughly $550 to $700 (see the report, section 6.4).
- The locked **test partition is blocked in code**: `sample()` and `pilot` refuse it. Remove that guard only for the final frozen run, and run it once.
- Benchmark Docker images are deleted by exact tag after each round and run. Nothing else on your machine is touched.
- The API key is written into containers via a short-lived 600-mode file and scrubbed from everything under `runs/` and `results/`.

## Tips
- **Docker CPU load:** the amd64 images run under emulation. In Docker Desktop, limit CPUs (Settings → Resources) and try "Use Rosetta for x86_64/amd64 emulation" (Settings → General).
- **Disk:** if Docker runs low, `docker images | grep -E 'reviewbench|c-crab-benchmark'` shows any leftover benchmark images; remove them by exact tag.

## Layout
| File | Purpose |
|---|---|
| `data.py` | Census, PR types, repository-disjoint splits |
| `reviewer.py` | LLM reviewer (findings JSON) |
| `harness.py` | Docker repair-and-test runner, spend ledger, cache, image cleanup, `pilot` |
| `evolve.py` | Skill library, retrieval, sanitizer, optimizer, gate, `static` and `evolve` CLIs |
| `analyze.py` | Released-tool scoring, bootstrap CI, permutation test |
| `report_numbers.py` | Summary numbers and figure for the report |
| `test_core.py` | Offline tests |
| `results/` | Committed outputs and the report draft |
| `runs/` | Local only: ledger, cache, raw logs (gitignored) |
| `vendor/c-crab/` | c-CRAB dataset and harness (submodule pinned at `856dfa3`, do not edit) |
