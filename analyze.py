"""Released-tool pass rates on the 234 stage-4 comments, overall and per partition.
Logic mirrors vendor filter_results_eval_by_resolved_comments.py: keep comments with resolved=True in
agent_resolution_combined, matched by (instance, comment_text); a tool with no result for a kept
comment counts as failed."""
import csv, json, zipfile
from pathlib import Path

import data

ROOT = Path(__file__).parent
RUNS = ROOT / "runs/released"
RAW = ROOT / "vendor/c-crab/raw_results_compressed"
TOOLS = ["claude-code", "codex", "devin", "pr-agent"]
PUBLISHED = {"claude-code": 32.1, "devin": 24.8, "pr-agent": 23.1, "codex": 20.1, "union": 41.5}


def extract():
    for z in [f"results_eval_{t}" for t in TOOLS] + ["agent_resolution_combined"]:
        if not (RUNS / z).exists():
            zipfile.ZipFile(RAW / f"{z}.zip").extractall(RUNS)


def results(d):
    return [json.loads(p.read_text()) for p in sorted((RUNS / d).glob("*/result.json"))]


def main():
    extract()
    stage4 = {r["instance_id"] for r in data.load(data.STAGES[-1])}
    keep = {}  # instance -> set of resolved comment_texts
    for d in results("agent_resolution_combined"):
        if d["instance_id"] in stage4:
            keep[d["instance_id"]] = {r["comment_text"] for r in d["results"]
                                      if r.get("resolved") is True and r.get("comment_text") is not None}
    assert set(keep) == stage4, "stage4 instances missing from resolution data"
    keys = [(i, t) for i, ts in sorted(keep.items()) for t in sorted(ts)]
    passed = {}  # tool -> set of keys
    for tool in TOOLS:
        passed[tool] = set()
        for d in results(f"results_eval_{tool}"):
            for r in d["results"]:
                k = (d["instance_id"], r.get("comment_text"))
                if k[1] in keep.get(k[0], ()) and r.get("test_passed") is True:
                    passed[tool].add(k)
    passed["union"] = set().union(*passed.values())

    splits = json.load(open(ROOT / "results/splits.json"))["instance_ids"]
    out = {"n_comments": len(keys)}
    for name, ids in [("all", None)] + list(splits.items()):
        ids = set(ids) if ids else None
        ks = [k for k in keys if ids is None or k[0] in ids]
        out[name] = {"n": len(ks)}
        for t in TOOLS + ["union"]:
            p = sum(k in passed[t] for k in ks)
            out[name][t] = {"passed": p, "rate": round(100 * p / len(ks), 1)}
    out["published_rate"] = PUBLISHED
    json.dump(out, open(ROOT / "results/released_tools.json", "w"), indent=2)
    with open(ROOT / "results/released_tools.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["partition", "n_comments"] + [f"{t}_{x}" for t in TOOLS + ["union"] for x in ("passed", "rate")])
        for name in ["all"] + list(splits):
            w.writerow([name, out[name]["n"]] + [out[name][t][x] for t in TOOLS + ["union"] for x in ("passed", "rate")])
    print(open(ROOT / "results/released_tools.csv").read())
    print("published:", PUBLISHED)


if __name__ == "__main__":
    main()
