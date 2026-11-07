# Scoped Skill Evolution for Code Review Agents

CSE 572 (Data Mining), Group 22. Does organizing evolved review skills into **global + PR-type-scoped tiers** improve held-out pass rate on [c-CRAB](https://github.com/c-CRAB-Benchmark/dataset) compared with a single global skill library, when both use the same validation gate?

How it works: an LLM reviewer reads a PR diff plus retrieved skills and returns up to 5 findings. Claude Code applies the findings inside the PR's Docker image, and c-CRAB's executable tests say pass or fail. An optimizer sees sanitized pass/fail feedback and proposes up to 2 skill edits per round. An edit is accepted only if it gains at least 1 selection PR and regresses at most 1.

**Model:** everything (reviewer, optimizer, repair agent) uses `qwen36-27b`, hosted by ASU at `https://openai.rc.asu.edu/v1`. Claude Code only speaks the Anthropic API, so the harness starts a local LiteLLM proxy that translates to the ASU endpoint (see "How the model is wired").

**Status:** progress-report stage. The pipeline runs end to end; the experiments so far are small. The numbers in `results/` and the report draft come from earlier runs with Claude Sonnet 4.6 and have **not** been re-run on `qwen36-27b`; re-run the static baseline before comparing. See `results/progress_report_draft.md`.

## Requirements
- Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/)
- Docker. The c-CRAB images are amd64-only, so on Apple Silicon they run under emulation (slow and CPU-heavy; see Tips)
- git
- An ASU Voyager API key. If the endpoint is unreachable, try toggling the ASU VPN; it worked here with the VPN off
- Docker Desktop (the containers reach the proxy at `host.docker.internal`)
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
VOYAGER_API_KEY=your-key-here
```

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

## How the model is wired
- `reviewer.py` and the optimizer call `openai/qwen36-27b` through LiteLLM with thinking disabled (`chat_template_kwargs`), since this is a reasoning model that otherwise spends its token budget on thinking and returns empty content.
- The repair agent is Claude Code inside the container. `harness.start_proxy()` (called by the CLIs) starts a LiteLLM proxy on `127.0.0.1:4000` that exposes the Anthropic API and forwards to ASU, and stops it at exit. Config is generated at `runs/proxy.yaml`.
- The proxy must use the `hosted_vllm/` provider: with `openai/` the streamed tool-call arguments are dropped and the agent loops on empty tool calls.
- `proxy_hooks.py` merges Claude Code's multiple system messages into one, because Qwen's chat template accepts only a single system message.
- The ASU key stays on the host. Containers get only a random per-run token that the local proxy accepts.

## Cost and safety
- Prices are set in `reviewer.py` (`PRICE_IN`, `PRICE_OUT`, $ per 1M tokens, currently 0.027 / 1.49). Confirm they match your hosting terms, or set both to 0 if it is free. Repair cost is computed from token usage at these prices, not from Claude Code's own cost field.
- Every paid call checks a spend ledger (`runs/ledger.jsonl`) first. The hard cap is `CAP_USD = 22.0` in `harness.py`, and a run stops cleanly when it would exceed it. The ledger is shared across models, so old spend counts toward the cap; move the file aside to reset it.
- Results are cached under `runs/cache/` by content, so re-running the same step is free. A run interrupted by Ctrl-C or the cap can be started again.
- Measured on `qwen36-27b`: a pilot PR cost about $0.002 (13k input, 0.3k output tokens for the repair). The Sonnet 4.6 figure used in the report ($0.29 per PR evaluation) no longer applies.
- The locked **test partition is blocked in code**: `sample()` and `pilot` refuse it. Remove that guard only for the final frozen run, and run it once.
- Benchmark Docker images are deleted by exact tag after each round and run. Nothing else on your machine is touched.
- The key is scrubbed from everything written under `runs/` and `results/`.

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
| `proxy_hooks.py` | LiteLLM proxy hook: merges system messages for Qwen |
| `test_core.py` | Offline tests |
| `results/` | Committed outputs and the report draft |
| `runs/` | Local only: ledger, cache, raw logs (gitignored) |
| `vendor/c-crab/` | c-CRAB dataset and harness (submodule pinned at `856dfa3`, do not edit) |
