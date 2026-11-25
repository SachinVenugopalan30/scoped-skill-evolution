"""Figures for the locked-test analysis.

Reads `results/analysis/final_summary.json` (produced by analyze_final.py) and, optionally,
`results/analysis/evolution_summary.json`, and writes:

  fig_test_comparison.png  comment-level test pass rate per condition with the 95% repository-clustered
                           bootstrap CI (errors counted as failed);
  fig_transfer.png         selection vs test pass rate per condition (transfer diagnostic, not held-out
                           performance for the selection point), annotated with the gap and accepted edits;
  fig_cost.png             tokens and wall-clock seconds per passing review (PR-level all-pass), with
                           "n/a" markers for conditions without a passing review.

Only aggregated result files are read. Nothing under the test split is touched: if the files are absent
the script prints that and exits 0. `--dry-run` prints the numbers it would plot and writes nothing.
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
AN = ROOT / "results/analysis"

PREFERRED_ORDER = ["static", "global", "scoped"]
COLORS = {"static": "#7f7f7f", "global": "#1f77b4", "scoped": "#2ca02c", "no-gate": "#ff7f0e"}
FALLBACK_COLORS = ["#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22"]
DPI = 200


def _color(cond, i):
    return COLORS.get(cond) or FALLBACK_COLORS[i % len(FALLBACK_COLORS)]


def _ordered(conditions):
    return [c for c in PREFERRED_ORDER if c in conditions] + \
           [c for c in conditions if c not in PREFERRED_ORDER]


def _fmt_rate(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _fmt_int(x):
    return "n/a" if x is None else f"{x:,.0f}"


def _load(path):
    try:
        return json.loads(path.read_text())
    except Exception as ex:
        print(f"warning: cannot read {path} ({ex})")
        return None


def _evo_enrichment(summary_obj, conditions):
    """Optional per-condition evolution metrics from evolution_summary.json (tolerant to shape)."""
    if not isinstance(summary_obj, (dict, list)):
        return {}
    try:
        import analyze_final as af   # reuse the tolerant round-record extractor
    except Exception as ex:          # e.g. numpy missing: figures must still be producible
        print(f"warning: cannot reuse analyze_final for evolution_summary.json ({ex}); ignoring it")
        return {}
    out = {}
    for cond in conditions:
        try:
            rounds = af._rounds_for(summary_obj, cond)
        except Exception:
            rounds = []
        if rounds:
            out[cond] = af._evolution_metrics(rounds, "results/analysis/evolution_summary.json")
    return out


# ---------------------------------------------------------------- figures
def fig_test_comparison(summary, path):
    metrics = summary["condition_metrics"]
    conds = _ordered(summary.get("conditions") or list(metrics))
    fig, ax = plt.subplots(figsize=(7.4, 4.2))
    xs = range(len(conds))
    for i, c in enumerate(conds):
        m = metrics[c]
        rate = m.get("pass_rate")
        lo, hi = (m.get("ci95") or {}).get("lo"), (m.get("ci95") or {}).get("hi")
        y = 100 * rate if rate is not None else 0.0
        err = None
        if rate is not None and lo is not None and hi is not None:
            err = [[max(0.0, y - 100 * lo)], [max(0.0, 100 * hi - y)]]
        ax.bar(i, y, width=0.62, color=_color(c, i), alpha=0.85,
               yerr=err, capsize=4, ecolor="#333333", error_kw={"lw": 1.0})
        label = f"{_fmt_rate(rate)}\nn={m.get('n_comments')} comments"
        if m.get("n_errors"):
            label += f"\n{m['n_errors']} err"
        top = y + (err[1][0] if err else 0)
        ax.text(i, top + 1.5, label, ha="center", va="bottom", fontsize=8)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(conds, fontsize=9)
    ax.set_ylabel("Comment pass rate (%)")
    tops = []
    for c in conds:
        ci = metrics[c].get("ci95") or {}
        tops.append(100 * (ci.get("hi") if ci.get("hi") is not None
                           else (metrics[c].get("pass_rate") or 0)))
    ax.set_ylim(0, max(100.0, max(tops) + 16))
    ax.set_title("Locked test partition: comment-level pass rate (errors as failed)", fontsize=10)
    cfg = summary.get("config") or {}
    footer = (f"Repository-clustered bootstrap, {cfg.get('bootstrap_resamples', '?')} resamples, "
              f"95% percentile CI; {len(conds)} conditions, errors counted as failed.")
    primary = summary.get("primary_contrast")
    if isinstance(primary, dict) and primary.get("a"):
        mc = primary.get("mcnemar") or {}
        diff, perm = primary.get("diff_pp"), primary.get("permutation_p")
        diff_s = f"{diff:+.1f}" if isinstance(diff, (int, float)) else "n/a"
        perm_s = f"{perm:.3f}" if isinstance(perm, (int, float)) else "n/a"
        footer += (f"\nPrimary: {primary['a']} - {primary['b']} = {diff_s} pp, "
                   f"permutation p={perm_s}, McNemar p={mc.get('p', float('nan')):.3g} "
                   f"(discordant {mc.get('discordant', '?')}).")
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.24 if "\n" in footer else 0.15)
    fig.text(0.01, 0.01, footer, fontsize=6.5, color="#444444", va="bottom")
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def fig_transfer(summary, evo, path):
    metrics = summary["condition_metrics"]
    conds = _ordered(summary.get("conditions") or list(metrics))
    fig, ax = plt.subplots(figsize=(7.4, 4.4))
    any_selection = False
    for i, c in enumerate(conds):
        m = metrics[c]
        t = m.get("selection_transfer") or {}
        test = t.get("test_rate")
        sel = t.get("selection_rate")
        col = _color(c, i)
        if test is not None:
            ci = m.get("ci95") or {}
            lo, hi = ci.get("lo"), ci.get("hi")
            err = None
            if lo is not None and hi is not None:
                err = [[max(0.0, 100 * test - 100 * lo)], [max(0.0, 100 * hi - 100 * test)]]
            ax.errorbar(i, 100 * test, yerr=err, fmt="o", ms=8, color=col, capsize=4,
                        label="test" if i == 0 else None, zorder=3)
        sx = i + 0.13   # offset the selection marker so the dashed gap segment is not hidden by the test CI
        if sel is not None:
            any_selection = True
            ax.plot(sx, 100 * sel, "X", ms=8, color=col, alpha=0.75, zorder=3)
            if test is not None:
                ax.plot([sx, sx], [100 * sel, 100 * test], color=col, lw=1.2, ls=(0, (3, 2)), zorder=2)
                gap = t.get("gap_pp") if t.get("gap_pp") is not None else 100 * (test - sel)
                ax.annotate(f"gap {gap:+.1f} pp", (sx, 100 * (sel + test) / 2),
                            textcoords="offset points", xytext=(8, 0), fontsize=7.5, color="#333333")
            else:
                ax.annotate("test n/a", (sx, 100 * sel), textcoords="offset points",
                            xytext=(8, 0), fontsize=7, color="#777777")
        else:
            ax.annotate("selection n/a", (i, 100 * (test or 0)), textcoords="offset points",
                        xytext=(-8, 8), ha="right", fontsize=7, color="#777777")
        edits = (m.get("evolution") or {}).get("accepted_edits")
        if edits is None and c in evo:
            edits = evo[c].get("accepted_edits")
        if edits:
            ax.annotate(f"{edits} accepted edit{'s' if edits != 1 else ''}",
                        (i, 2), ha="center", fontsize=6.5, color="#555555")
    ax.set_xticks(range(len(conds)))
    ax.set_xticklabels(conds, fontsize=9)
    ax.set_ylabel("Pass rate (%)")
    ax.set_ylim(0, 100)
    ax.set_title("Selection vs locked-test pass rate (transfer diagnostic)", fontsize=10)
    handles = [plt.Line2D([], [], marker="o", ls="none", color="#444444", ms=8, label="test (95% CI)")]
    labels = ["test (95% CI)"]
    if any_selection:
        handles += [plt.Line2D([], [], marker="X", ls="none", color="#444444", alpha=0.75, ms=8),
                    plt.Line2D([], [], color="#444444", lw=1.2, ls=(0, (3, 2)))]
        labels += ["selection (frozen library)", "selection -> test gap"]
    ax.legend(handles, labels, fontsize=7.5, loc="upper right", frameon=True)
    note = ("Selection is never reported as held-out performance: it is the partition the gate and the\n"
            "optimizer saw. Dashed segments show the selection->test gap; bars are the test 95% CI.")
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.16)
    fig.text(0.01, 0.01, note, fontsize=6.5, color="#444444", va="bottom")
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def fig_cost(summary, path):
    metrics = summary["condition_metrics"]
    conds = _ordered(summary.get("conditions") or list(metrics))
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.0))

    def panel(ax, key, title, ylabel):
        vals = [(metrics[c].get("cost") or {}).get(key) for c in conds]
        present = [v for v in vals if v]
        for i, (c, v) in enumerate(zip(conds, vals)):
            ax.bar(i, v or 0.0, width=0.62, color=_color(c, i), alpha=0.85)
        ax.set_ylim(0, (max(present) if present else 1.0) * 1.28)
        for i, v in enumerate(vals):
            if v is None:
                ax.text(i, (max(present) if present else 1.0) * 0.03, "n/a",
                        ha="center", va="bottom", fontsize=7.5, color="#aa2222")
            else:
                ax.text(i, v, _fmt_int(v), ha="center", va="bottom", fontsize=7.5)
        ax.set_xticks(range(len(conds)))
        ax.set_xticklabels(conds, fontsize=9)
        ax.set_title(title, fontsize=9.5)
        ax.set_ylabel(ylabel)

    panel(axes[0], "tokens_per_passing_review",
          "Tokens per passing review", "Total tokens")
    durations = [(metrics[c].get("cost") or {}).get("duration_recorded") for c in conds]
    if any(durations):
        panel(axes[1], "seconds_per_passing_review",
              "Wall-clock seconds per passing review", "Seconds")
    else:
        axes[1].axis("off")
        axes[1].text(0.5, 0.55, "duration_s is 0 throughout the scored outputs\n"
                                "(wall-clock not recorded in this run)",
                     ha="center", va="center", fontsize=9, color="#555555")
        axes[1].set_title("Wall-clock cost", fontsize=9.5)

    n_pass = {c: (metrics[c].get("pr_all_pass") or {}).get("n_all_pass") for c in conds}
    footer = ("Per passing review = per test PR whose reference comments all pass (errors as failed). "
              f"Passing reviews: " + ", ".join(f"{c}={n_pass[c]}" for c in conds) + ".")
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.13)
    fig.text(0.01, 0.01, footer, fontsize=6.5, color="#444444", va="bottom")
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Figures for the locked-test analysis.")
    ap.add_argument("--out-dir", default=str(AN), help="dir holding final_summary.json and receiving PNGs "
                                                       "(default results/analysis)")
    ap.add_argument("--dry-run", action="store_true", help="print the numbers, write no files")
    a = ap.parse_args(argv)
    out_dir = Path(a.out_dir)

    summary_path = out_dir / "final_summary.json"
    if not summary_path.exists():
        print(f"No {summary_path}: run analyze_final.py once the locked test results exist.")
        print("Figures were not produced (no test-partition access).")
        return 0
    summary = _load(summary_path)
    if not summary or not isinstance(summary.get("condition_metrics"), dict) or not summary["condition_metrics"]:
        print(f"{summary_path} has no condition metrics; nothing to plot.")
        return 0

    metrics = summary["condition_metrics"]
    conds = _ordered(summary.get("conditions") or list(metrics))
    evo = {}
    evo_path = out_dir / "evolution_summary.json"
    if evo_path.exists():
        evo = _evo_enrichment(_load(evo_path), conds)

    targets = [("fig_test_comparison.png", lambda p: fig_test_comparison(summary, p)),
               ("fig_transfer.png", lambda p: fig_transfer(summary, evo, p)),
               ("fig_cost.png", lambda p: fig_cost(summary, p))]

    print(f"{'condition':<10} {'test':>7} {'sel':>7} {'gap pp':>7} "
          f"{'tok/pass':>10} {'s/pass':>8}")
    for c in conds:
        m = metrics[c]
        t = m.get("selection_transfer") or {}
        cost = m.get("cost") or {}
        gap = t.get("gap_pp")
        secs = cost.get("seconds_per_passing_review")
        print(f"{c:<10} {_fmt_rate(t.get('test_rate')):>7} {_fmt_rate(t.get('selection_rate')):>7} "
              f"{(f'{gap:+.1f}' if gap is not None else 'n/a'):>7} "
              f"{_fmt_int(cost.get('tokens_per_passing_review')):>10} "
              f"{(f'{secs:.0f}' if secs else 'n/a'):>8}")

    if a.dry_run:
        print("\n--dry-run: would write " + ", ".join(str(out_dir / n) for n, _ in targets))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in targets:
        fn(out_dir / name)
        print(f"wrote {out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
