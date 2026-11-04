"""Numbers and figure for the progress report. Reads results/ and runs/ledger.jsonl; writes results/summary.json and
results/fig_static_vs_released.png. Everything in the report's tables comes from summary.json."""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import analyze

ROOT = Path(__file__).parent
R = ROOT / "results"


def main():
    st = json.load(open(R / "static_development.json"))
    ids = [p["instance_id"] for p in st["prs"]]
    rows = st["comments"]  # errored PRs already counted as failed
    n, k = len(rows), sum(bool(r["passed"]) for r in rows)
    ci = analyze.bootstrap_ci(rows)

    keys, passed = analyze.released_passed()
    same = [x for x in keys if x[0] in set(ids)]
    rel = {t: sum(x in passed[t] for x in same) for t in analyze.TOOLS + ["union"]}
    # PR-level: all of a PR's comments pass
    pr_all = lambda t: sum(all(x in passed[t] for x in same if x[0] == i) for i in ids)
    ours_pr = sum(1 for p in st["prs"] if p["passed"] is True)

    ledger = [json.loads(l) for l in open(ROOT / "runs/ledger.jsonl")]
    spent = sum(r["cost"] for r in ledger)
    by = {}
    for r in ledger:
        by.setdefault(r["kind"], []).append(r["cost"])
    per_pr = (sum(by["review"]) + sum(by["repair"])) / len(by["repair"])

    evo = {}
    for c in ("global", "scoped"):
        rounds = [json.loads(l) for l in (R / f"evolution/{c}.jsonl").read_text().splitlines()]
        r1 = [x for x in rounds if "accept" in x][-1]
        evo[c] = {"accepted": r1["accept"], "gained": r1["gained"], "regressed": r1["regressed"],
                  "sel_base": r1["sel_base"], "sel_new": r1["sel_new"],
                  "edit_scopes": [e["scope"] for e in r1["edits"]], "cost_usd": r1.get("cost_usd")}

    out = {
        "static_dev": {"n_prs": len(ids), "n_comments": n, "passed": k, "rate": k / n, "ci95": [ci["lo"], ci["hi"]],
                       "n_errors": st["summary"]["n_errors"], "pass_rate_excl_errors": st["summary"]["pass_rate_excl_errors"],
                       "excluded": {a: b["n"] for a, b in st["excluded"].items()}},
        "released_same_comments": {"n_comments": len(same), "passed": rel, "prs_all_pass": {t: pr_all(t) for t in rel},
                                   "ours_prs_all_pass": ours_pr, "n_prs": len(ids)},
        "evolution": evo,
        "cost": {"total_usd": round(spent, 3), "cap_code": 22.0, "n_repairs": len(by["repair"]),
                 "n_reviews": len(by["review"]), "n_optimizer": len(by.get("optimizer", [])),
                 "mean_per_pr_eval_usd": round(per_pr, 3), "mean_repair_usd": round(sum(by["repair"]) / len(by["repair"]), 3),
                 "mean_review_usd": round(sum(by["review"]) / len(by["review"]), 3)},
    }
    json.dump(out, open(R / "summary.json", "w"), indent=2)

    labels = ["Ours (static)", "Claude Code", "Devin", "PR-Agent", "Codex", "All four"]
    vals = [k] + [rel[t] for t in ("claude-code", "devin", "pr-agent", "codex", "union")]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    colors = ["#1f5fa8"] + ["#9aa5b1"] * 5
    assert len(same) == n, (len(same), n)  # released-tool comments must line up with ours
    bars = ax.bar(labels, [100 * v / n for v in vals], color=colors)
    for i, (b, v) in enumerate(zip(bars, vals)):
        top = 100 * ci["hi"] if i == 0 else b.get_height()  # keep the label clear of the CI whisker
        ax.text(b.get_x() + b.get_width() / 2, top + 1, f"{v}/{n}", ha="center", fontsize=9)
    ax.errorbar(0, 100 * k / n, yerr=[[100 * (k / n - ci["lo"])], [100 * (ci["hi"] - k / n)]], color="black", capsize=4, lw=1)
    ax.set_ylabel("Comment pass rate (%)")
    ax.set_title(f"Same {n} dev comments ({len(ids)} PRs), one run", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_ylim(0, 100)
    plt.setp(ax.get_xticklabels(), fontsize=8)
    fig.tight_layout()
    fig.savefig(R / "fig_static_vs_released.png", dpi=200)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
