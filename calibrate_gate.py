"""Null-gate calibration from fresh A/A repeats on the selection partition.

Protocol §9.1: compare every repeat against repeat 1; any pass/fail difference is noise.
tau = max(1, ceil(95th percentile of |net|)) where net = gained - regressed on PR-level
pass/fail. All-pairs nets are reported as a sensitivity, not used for the frozen gate.

Hard-fails on the wrong partition/condition, incomplete repeats, duplicate salts, or fewer
than 5 repeats (unless --allow-fewer is given for a diagnostic). Never reads the test split.
"""
import argparse, json, math
from itertools import combinations
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
CAL = ROOT / "results/calibration"
EXPECTED_N = 34


def load_repeat(path):
    try:
        d = json.loads(path.read_text())
    except Exception as e:
        raise SystemExit(f"{path.name}: cannot read JSON: {type(e).__name__}: {e}")
    for k in ("partition", "condition", "n_done", "n_requested", "prs", "summary", "salt"):
        if k not in d:
            raise SystemExit(f"{path.name}: missing required field {k!r}")
    if d["partition"] != "selection" or d["condition"] != "static":
        raise SystemExit(f"{path.name}: expected static/selection, got {d['condition']}/{d['partition']}")
    if d["n_done"] != EXPECTED_N or d["n_requested"] != EXPECTED_N:
        raise SystemExit(f"{path.name}: expected {EXPECTED_N} PRs, got {d['n_done']}/{d['n_requested']}")
    if not isinstance(d["salt"], str) or not d["salt"]:
        raise SystemExit(f"{path.name}: missing salt (was this a calibration draw?)")
    if not isinstance(d["prs"], list) or len(d["prs"]) != EXPECTED_N:
        raise SystemExit(f"{path.name}: expected {EXPECTED_N} PR records, got {len(d.get('prs') or [])}")
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(CAL))
    ap.add_argument("--allow-fewer", action="store_true",
                    help="diagnostic only: accept fewer than 5 repeats (the frozen protocol requires 5)")
    a = ap.parse_args()
    cal = Path(a.dir)
    files = sorted(cal.glob("calib_*.json"))
    if not a.allow_fewer and len(files) < 5:
        raise SystemExit(f"protocol requires 5 A/A repeats, found {len(files)} in {cal} (use --allow-fewer only for diagnostics)")
    if len(files) < 2:
        raise SystemExit(f"need at least 2 calibration repeats, found {len(files)} in {cal}")

    runs, summaries = {}, {}
    for p in files:
        d = load_repeat(p)
        if d["salt"] in runs:
            raise SystemExit(f"duplicate salt {d['salt']!r} in {p.name}")
        runs[d["salt"]] = {r["instance_id"]: r.get("passed") for r in d["prs"]}
        summaries[d["salt"]] = {"n_prs": d.get("n_done"), "errors": d["summary"]["n_errors"],
                                "passed": d["summary"]["passed"], "comments": d["summary"]["n_comments"],
                                "rate_errors_as_failed": d["summary"]["pass_rate_errors_as_failed"],
                                "salt": d.get("salt")}
    salts = list(runs)
    ids = sorted(set.intersection(*[set(v) for v in runs.values()]))
    if len(ids) != EXPECTED_N:
        raise SystemExit(f"repeats do not share the same {EXPECTED_N} PR ids (common={len(ids)})")

    def net(a, b):
        common = [i for i in ids if runs[a][i] is not None and runs[b][i] is not None]
        gained = sum(1 for i in common if not runs[a][i] and runs[b][i])
        regressed = sum(1 for i in common if runs[a][i] and not runs[b][i])
        return {"a": a, "b": b, "common": len(common), "gained": gained,
                "regressed": regressed, "net": gained - regressed}

    primary = [net(salts[0], b) for b in salts[1:]]  # PROTOCOL §9.1: against repeat 1
    all_pairs = [net(a, b) for a, b in combinations(salts, 2)]
    for label, pairs in (("primary", primary), ("all_pairs", all_pairs)):
        for p in pairs:
            if p["common"] != EXPECTED_N:
                raise SystemExit(f"{label} pair {p['a']} vs {p['b']} has {p['common']}/{EXPECTED_N} comparable PRs")

    prim_nets = [p["net"] for p in primary]
    all_nets = [p["net"] for p in all_pairs]
    p95 = float(np.percentile(np.abs(prim_nets), 95))
    tau = max(1, math.ceil(p95))
    p95_all = float(np.percentile(np.abs(all_nets), 95))
    tau_all = max(1, math.ceil(p95_all))

    flips, valid = 0, 0
    per_pr = {}
    for i in ids:
        vals = [runs[s][i] for s in salts if runs[s][i] is not None]
        per_pr[i] = {"valid": len(vals), "passed": sum(v is True for v in vals), "flip": len(set(vals)) > 1}
        if len(vals) >= 2:
            valid += 1
            flips += per_pr[i]["flip"]

    out = {
        "n_repeats": len(files),
        "n_selection_prs": len(ids),
        "salts": salts,
        "summaries": summaries,
        "primary_pairs": primary,
        "all_pairs": all_pairs,
        "nets_primary": prim_nets,
        "nets_all": all_nets,
        "net_abs_p95_primary": p95,
        "net_abs_p95_all": p95_all,
        "flip_rate": flips / valid if valid else None,
        "n_flipped_prs": flips,
        "n_valid_prs": valid,
        "tau": tau,
        "tau_all_pairs_sensitivity": tau_all,
        "gate": {"min_gain": tau, "max_regress": 1},
        "threshold_rule": "tau = max(1, ceil(95th percentile of |net| vs repeat 1)); all-pairs reported as sensitivity",
        "per_pr": per_pr,
        "expected_n": EXPECTED_N,
    }
    cal.mkdir(parents=True, exist_ok=True)
    (cal / "summary.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in ("n_repeats", "n_selection_prs", "salts", "nets_primary",
                                           "net_abs_p95_primary", "tau", "tau_all_pairs_sensitivity",
                                           "flip_rate", "n_flipped_prs", "n_valid_prs", "gate")}, indent=2))
    print("wrote", cal / "summary.json")


if __name__ == "__main__":
    main()
