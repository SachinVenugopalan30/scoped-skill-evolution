"""Paper figures for report/latex: the calibration strip and the per-round gate plot.

  uv run python report/make_figures.py

Reads only results/calibration/*.json and results/evolution/*.jsonl (development/selection data).
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Rectangle

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "report/latex"
PASS, FAIL, TEXT, MUTED = "#257de0", "#d9dde3", "#191919", "#666666"
GAIN, LOSS = "#257de0", "#f47230"
plt.rcParams.update({"font.size": 7, "font.family": "DejaVu Sans"})


def calibration_strip():
    runs = [("Baseline", "baseline_selection.json")] + [(f"Repeat {k}", f"calib_{k}.json") for k in range(1, 6)]
    R = [{r["instance_id"]: r.get("passed") for r in json.loads((ROOT / "results/calibration" / f).read_text())["prs"]}
         for _, f in runs]
    ids = sorted(R[0], key=lambda i: (-sum(r[i] is True for r in R), sum(r[i] is None for r in R), i))
    fig, ax = plt.subplots(figsize=(7.0, 1.55), dpi=300)
    g = 0.1
    for x, i in enumerate(ids):
        for y, r in enumerate(R):
            v = r[i]
            kw = dict(facecolor="white", edgecolor=MUTED, hatch="/////", lw=0.4) if v is None else \
                dict(facecolor=PASS if v else FAIL, lw=0)
            ax.add_patch(Rectangle((x + g / 2, y + g / 2), 1 - g, 1 - g, **kw))
        ax.text(x + 0.5, len(R) + 0.35, str(sum(r[i] is True for r in R)), ha="center", va="center", fontsize=5.5, color=TEXT)
    always = sum(all(r[i] is True for r in R) for i in ids)
    mixed = sum(any(r[i] is True for r in R) and not all(r[i] is True for r in R) for i in ids)
    for a, b, lab in ((0, always, f"{always} always pass"), (always, always + mixed, f"{mixed} change outcome"),
                      (always + mixed, len(ids), f"{len(ids) - always - mixed} never pass")):
        ax.plot([a + 0.15, b - 0.15], [-0.35, -0.35], color=TEXT, lw=0.8)
        ax.text((a + b) / 2, -0.85, lab, ha="center", va="center", fontsize=6.5, color=TEXT)
    ax.text(-0.3, len(R) + 0.35, "passes of 6:", ha="right", va="center", fontsize=5.5, color=MUTED)
    ax.set_xlim(-0.1, len(ids)); ax.set_ylim(len(R) + 0.8, -1.2)
    ax.set_yticks([y + 0.5 for y in range(len(R))]); ax.set_yticklabels([n for n, _ in runs], fontsize=6, color=TEXT)
    ax.set_xticks([]); ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.legend(handles=[Patch(facecolor=PASS, label="all tests pass"), Patch(facecolor=FAIL, label="fails"),
                       Patch(facecolor="white", edgecolor=MUTED, hatch="/////", label="infra error")],
              loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=3, fontsize=6, frameon=False, handlelength=1.1)
    fig.tight_layout(pad=0.2)
    fig.savefig(OUT / "fig_calibration.pdf", bbox_inches="tight")
    plt.close(fig)


def latest_rounds(cond):
    by = {}
    for line in (ROOT / f"results/evolution/{cond}.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("round") is not None and "accept" in r:
            by[r["round"]] = r
    return [by[k] for k in sorted(by)]


def gate():
    conds = [("global", "Global"), ("scoped", "Scoped")]
    fig, axes = plt.subplots(1, 2, figsize=(3.4, 1.9), dpi=300, sharey=True, gridspec_kw={"width_ratios": [2, 4]})
    for ax, (c, title) in zip(axes, conds):
        rs = latest_rounds(c)
        xs = [r["round"] for r in rs]
        ax.bar([x - 0.18 for x in xs], [r["gained"] for r in rs], 0.34, color=GAIN, label="gained")
        ax.bar([x + 0.18 for x in xs], [-r["regressed"] for r in rs], 0.34, color=LOSS, label="regressed")
        ax.plot(xs, [r["net"] for r in rs], color=TEXT, marker="D", ms=3, lw=1, label="net")
        for r in rs:
            ax.text(r["round"], max(r["gained"], 0) + 0.45, ("acc." if r["accept"] else "rej.") + f" {r['net']:+d}",
                    ha="center", fontsize=5.5, color=TEXT)
        ax.axhline(1, color=MUTED, ls="--", lw=0.7)
        ax.axhline(0, color=TEXT, lw=0.6)
        ax.set_xticks(xs); ax.set_title(title, fontsize=7, color=TEXT)
        ax.set_xlabel("round", fontsize=6.5)
        ax.grid(axis="y", color="#eeeeee", lw=0.5); ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    axes[0].set_ylabel("selection PRs", fontsize=6.5)
    axes[0].set_ylim(-5.8, 3.2)
    h, l = axes[1].get_legend_handles_labels()
    fig.legend(h + [plt.Line2D([], [], color=MUTED, ls="--", lw=0.7)], l + ["gate: net ≥ 1"], loc="lower center",
               ncol=4, fontsize=5.5, frameon=False, bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.08, 1, 1), pad=0.3)
    fig.savefig(OUT / "fig_gate.pdf", bbox_inches="tight")
    plt.close(fig)


def evolution():
    """Left: development pass rate per round. Right: selection candidate vs current, against the A/A band."""
    cal = [json.loads((ROOT / f"results/calibration/calib_{k}.json").read_text())["summary"]["passed"] for k in range(1, 6)]
    lo, hi = min(cal) / 42 * 100, max(cal) / 42 * 100
    colors = {"global": GAIN, "scoped": LOSS}
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(3.4, 1.8), dpi=300)
    a1.axhline(26 / 107 * 100, color=MUTED, ls="--", lw=0.7, label="static")
    for c, dx in (("global", -0.07), ("scoped", 0.07)):  # small offset: rounds 1-2 coincide
        rs = latest_rounds(c)
        xs = [r["round"] + dx for r in rs]
        a1.plot(xs, [r["dev"]["passed"] / 107 * 100 for r in rs], marker="o", ms=3, lw=1.2, color=colors[c], label=c)
        a2.plot(xs, [r["sel_base"]["passed"] / 42 * 100 for r in rs], lw=1, color=colors[c], alpha=0.6)
        a2.scatter(xs, [r["sel_new"]["passed"] / 42 * 100 for r in rs], s=14, color=colors[c], zorder=3,
                   edgecolor="white", linewidth=0.6)
    a2.axhspan(lo, hi, color="#e9ecf1", zorder=0)
    a2.text(4.45, (lo + hi) / 2, "A/A range", rotation=90, va="center", ha="center", fontsize=5.5, color=MUTED)
    for ax, t in ((a1, "Development"), (a2, "Selection: candidate (dots)\nvs current (lines)")):
        ax.set_title(t, fontsize=6.5, color=TEXT); ax.set_xticks([1, 2, 3, 4]); ax.set_xlabel("round", fontsize=6.5)
        ax.set_xlim(0.6, 4.7); ax.grid(axis="y", color="#eeeeee", lw=0.5); ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    a1.set_ylabel("comment pass rate (%)", fontsize=6.5); a1.set_ylim(15, 30); a2.set_ylim(8, 36)
    a1.legend(fontsize=5.5, frameon=False, loc="lower left")
    fig.tight_layout(pad=0.3)
    fig.savefig(OUT / "fig_evolution.pdf", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    calibration_strip()
    gate()
    evolution()
    print("wrote", OUT / "fig_calibration.pdf", OUT / "fig_gate.pdf", OUT / "fig_evolution.pdf")
