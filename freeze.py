"""Write results/FROZEN.json (PROTOCOL.md §14) before the locked test pass.

Refuses a dirty tracked tree and refuses if any test-partition result already exists. Reads only development /
selection metadata, the round logs, the library snapshots, the calibration record and the ledger; the test partition
contributes its eligible PR ids (inputs only), never an outcome.

Usage:  uv run python freeze.py --test-salt test-1 [--dry-run]
Then commit results/FROZEN.json; the test guard accepts the frozen commit or any descendant that leaves the scored code
files unchanged.
"""
import argparse, json, subprocess, sys
from collections import Counter
from datetime import datetime, timezone

import evolve, harness, reviewer

ROOT = evolve.ROOT
CONDITIONS = ("global", "scoped")
FLAGS = {"dev_n": 87, "sel_n": 34, "rounds": 5, "gate_min_gain": 1, "gate_max_regress": 1, "stop_after_rejections": 2,
         "seed": 0, "salt": "static-v1", "amendments": ["A1", "A2", "A3", "A4", "A5"]}


def git(*args):
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True).stdout.strip()


def rounds_for(condition, upto):
    """Latest record per round number (re-runs supersede invalid records, as in analyze_evolution.py)."""
    by = {}
    for line in (ROOT / f"results/evolution/{condition}.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("round") is not None and "accept" in r:
            by[r["round"]] = r
    return [{"round": n, "accept": r["accept"], "gained": r["gained"], "regressed": r["regressed"], "net": r["net"],
             "note": r.get("note"), "edits": r["edits"], "run_id": r.get("run_id"),
             "dev_passed_comments": (r.get("dev") or {}).get("passed")}
            for n, r in sorted(by.items()) if n <= upto]


def build(test_salt):
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("tracked files are modified: commit first (a dirty tree is refused, PROTOCOL §14)")
    done = sorted(p.name for p in (ROOT / "results").glob("test_*.json")) + \
        sorted(p.name for p in (ROOT / "results/partial").glob("test_*.json"))
    if done:
        raise SystemExit(f"test-partition results already exist ({done}); a freeze must precede any test evaluation")
    libs, detail = {}, {}
    for c in CONDITIONS:
        n, lib = evolve.latest_snapshot(c)
        if lib is None:
            raise SystemExit(f"no library snapshot for {c}")
        lib = evolve.normalize_library(lib)
        rounds = rounds_for(c, n)
        if [r["round"] for r in rounds] != list(range(1, n + 1)):
            raise SystemExit(f"{c}: round records {[r['round'] for r in rounds]} do not cover snapshot round {n}")
        libs[c] = evolve.library_sha256(lib)
        detail[c] = {"snapshot": f"results/skills/{c}/round_{n}.json", "rounds_evaluated": n, "skills": lib,
                     "accepted": sum(r["accept"] for r in rounds), "rejected": sum(not r["accept"] for r in rounds),
                     "rounds": rounds}
    by, ex = evolve._eligible("test")
    test_ids = sorted(i for v in by.values() for i in v)
    cal = json.loads((ROOT / "results/calibration/summary.json").read_text())
    ledger = [json.loads(l) for l in (ROOT / "runs/ledger.jsonl").read_text().splitlines() if l.strip()]
    tok = Counter()
    for r in ledger:
        t = r.get("tokens") or {}
        tok[r["kind"]] += sum(v for k, v in t.items() if isinstance(v, (int, float)) and "tokens" in k)
    return {
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_sha": git("rev-parse", "HEAD"),
        "code_manifest": evolve._code_manifest(),
        "harness_version": harness.HARNESS_VERSION,
        "model": reviewer.MODEL, "repair_model": harness.REPAIR_MODEL, "endpoint": reviewer.API_BASE,
        "split_seed": json.loads((ROOT / "results/splits.json").read_text()).get("seed"),
        "development_ids": evolve.sample("development", FLAGS["dev_n"], FLAGS["seed"]),
        "selection_ids": evolve.sample("selection", FLAGS["sel_n"], FLAGS["seed"]),
        "test_ids": test_ids,
        "test_excluded": {k: v for k, v in ex.items()},
        "test_salt": test_salt,
        "calibration": {k: cal.get(k) for k in ("n_repeats", "salts", "nets_primary", "nets_all", "net_abs_p95_primary",
                                                "tau", "tau_all_pairs_sensitivity", "flip_rate", "n_flipped_prs")},
        "operating_gate": {"min_gain": FLAGS["gate_min_gain"], "max_regress": FLAGS["gate_max_regress"],
                           "why": "PROTOCOL A1: calibrated tau reported as sensitivity"},
        "final_run_flags": FLAGS,
        "libraries": libs,
        "library_detail": detail,
        "retrieval": {"budget_tokens": evolve.RETRIEVAL_BUDGET, "max_skills": evolve.MAX_SKILLS,
                      "order": "newest-first within scope", "dedup": True},
        "ledger_summary": {"entries": len(ledger), "tokens_by_stage": dict(tok)},
        "statement": "No test-partition evaluation has been run at freeze time.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-salt", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    out = build(a.test_salt)
    text = json.dumps(out, indent=2)
    if a.dry_run:
        print(text[:4000])
        return
    (ROOT / "results/FROZEN.json").write_text(text + "\n")
    print(f"wrote results/FROZEN.json: code {out['code_sha'][:12]}, libraries "
          f"{ {c: h[:12] for c, h in out['libraries'].items()} }, {len(out['test_ids'])} test ids")


if __name__ == "__main__":
    sys.exit(main())
