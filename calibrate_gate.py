"""Null-gate calibration from fresh A/A repeats on the selection partition.

Each calib_*.json is the same static (empty) library evaluated on the same 34 selection PRs
with a different salt. Any pass/fail difference between two repeats is noise. The gate
threshold is the 95th percentile of |net| = |gained - regressed| over all repeat pairs,
floored at 1 (PROTOCOL.md §9).
"""
import json, math
from itertools import combinations
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
CAL = ROOT / "results/calibration"


def main():
    files = sorted(CAL.glob("calib_*.json"))
    if len(files) < 2:
        raise SystemExit(f"need at least 2 calibration repeats, found {len(files)} in {CAL}")
    runs, summaries = {}, {}
    for p in files:
        d = json.loads(p.read_text())
        runs[p.stem] = {r["instance_id"]: r.get("passed") for r in d["prs"]}
        summaries[p.stem] = {"n_prs": d.get("n_done"), "errors": d["summary"]["n_errors"],
                             "passed": d["summary"]["passed"], "comments": d["summary"]["n_comments"],
                             "rate_errors_as_failed": d["summary"]["pass_rate_errors_as_failed"],
                             "salt": d.get("salt")}
    ids = sorted(set.intersection(*[set(v) for v in runs.values()]))

    pairs, nets = [], []
    for a, b in combinations(runs, 2):
        common = [i for i in ids if runs[a][i] is not None and runs[b][i] is not None]
        gained = sum(1 for i in common if not runs[a][i] and runs[b][i])
        regressed = sum(1 for i in common if runs[a][i] and not runs[b][i])
        net = gained - regressed
        pairs.append({"a": a, "b": b, "common": len(common), "gained": gained,
                      "regressed": regressed, "net": net})
        nets.append(net)

    # Per-PR flip rate: a PR flips if its pass/fail differs across valid repeats.
    flips, valid = 0, 0
    per_pr = {}
    for i in ids:
        vals = [runs[a][i] for a in runs if runs[a][i] is not None]
        per_pr[i] = {"valid": len(vals), "passed": sum(v is True for v in vals), "flip": len(set(vals)) > 1}
        if len(vals) >= 2:
            valid += 1
            flips += per_pr[i]["flip"]

    abs_nets = np.abs(nets) if nets else np.array([0])
    p95 = float(np.percentile(abs_nets, 95))
    tau = max(1, math.ceil(p95))
    out = {
        "n_repeats": len(runs),
        "n_selection_prs": len(ids),
        "summaries": summaries,
        "pairs": pairs,
        "nets": nets,
        "net_abs_p95": p95,
        "flip_rate": flips / valid if valid else None,
        "n_flipped_prs": flips,
        "n_valid_prs": valid,
        "tau": tau,
        "gate": {"min_gain": tau, "max_regress": 1},
        "threshold_rule": "tau = max(1, ceil(95th percentile of |net| over A/A repeat pairs))",
        "per_pr": per_pr,
    }
    CAL.mkdir(parents=True, exist_ok=True)
    (CAL / "summary.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in ("n_repeats", "n_selection_prs", "nets", "net_abs_p95",
                                           "flip_rate", "n_flipped_prs", "n_valid_prs", "tau", "gate")}, indent=2))
    print("wrote", CAL / "summary.json")


if __name__ == "__main__":
    main()
