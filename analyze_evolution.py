"""Evolution tables and figures from results/evolution/<condition>.jsonl.

Reads only the append-only round logs written by evolve.run_round (one JSON object per line, plus run headers and
aborted/stopped markers). Never touches the test partition, the network, Docker or litellm.

Writes (under --out-dir, default results/analysis/):
  evolution_summary.json   all numbers behind the report's evolution section
  evolution_table.csv      one row per evaluated round (dev + selection, gate, library, tokens, cost)
  fig_evolution.png        dev and selection curves, library size, cumulative accepted edits
  fig_gate.png             gained / regressed / net per round, per condition

Usage:
  uv run python analyze_evolution.py                 # both conditions
  uv run python analyze_evolution.py --dry-run       # show what would be written, write nothing
  uv run python analyze_evolution.py --conditions scoped --out-dir /tmp/ana
  uv run python analyze_evolution.py --evo-dir /tmp/evo-log   # testing aid; default results/evolution
"""
import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
EVO_DIR = ROOT / "results/evolution"
DEFAULT_OUT = ROOT / "results/analysis"
CONDITIONS = ("global", "scoped", "global-rich", "scoped-rich")
SCOPES = ("global", "bug_fix", "feature", "other")
COLORS = {"global": "#1f5fa8", "scoped": "#c0392b"}
FALLBACK_COLORS = ("#2e8b57", "#8e44ad", "#e67e22", "#16a085")
OUTPUT_NAMES = ("evolution_summary.json", "evolution_table.csv", "fig_evolution.png", "fig_gate.png")

ROW_FIELDS = [
    "condition", "round", "run_id", "gated", "edits_count", "edits", "accepted",
    "gained", "regressed", "net", "gate_excluded",
    "dev_n_prs", "dev_n_errors", "dev_pr_passed",
    "dev_pr_rate_errors_as_failed", "dev_pr_rate_complete_case",
    "dev_comment_passed", "dev_comment_total",
    "dev_comment_rate_errors_as_failed", "dev_comment_rate_excl_errors",
    "sel_base_pr_rate_errors_as_failed", "sel_base_pr_rate_complete_case",
    "sel_new_pr_rate_errors_as_failed", "sel_new_pr_rate_complete_case",
    "sel_base_comment_passed", "sel_base_comment_total",
    "sel_base_comment_rate_errors_as_failed", "sel_base_comment_rate_excl_errors",
    "sel_new_comment_passed", "sel_new_comment_total",
    "sel_new_comment_rate_errors_as_failed", "sel_new_comment_rate_excl_errors",
    "library_before", "library_after", "tokens", "duration_s", "cost_usd", "note",
]

ASSUMPTIONS = [
    "Only results/evolution/<condition>.jsonl is read (no gold/test data, no network, no Docker).",
    "A round number repeated in the log keeps its latest record (resume / re-run support); the last run header in the "
    "file is reported as the run metadata, and earlier headers are counted, not merged.",
    "evolve.summarize does not record PR-level pass counts, so selection PR-level rates are empty unless a future "
    "record adds passed_prs/prs; selection is reported at comment level (both errors-as-failed and complete-case).",
    "Complete-case denominators come from the recorded pass_rate_excl_errors values and are not recomputed.",
    "Regressions/net per accepted edit use gated accepted rounds only: ungated rounds record gained/regressed as null "
    "and are counted separately.",
    "Repo-clustered bootstrap / paired permutation (analyze.py) need per-comment row pairs, which round records do not "
    "store; evolution curves therefore report Wilson binomial intervals for the final round, and the primary "
    "test-partition inference remains in report_numbers.py/analyze.py.",
    "library_before/library_after are the sizes of the library lists stored in that record; a resumed run inherits the "
    "library from the earlier snapshot, so the curve stays continuous.",
]


# ---------- small helpers ----------
def _truncate(text, n=90):
    s = " ".join(str(text).split()) if text is not None else ""
    return s if len(s) <= n else s[:n - 1] + "…"


def _num(x):
    """float or None (bools/strings/lists are not numbers here)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def _int(x):
    v = _num(x)
    return int(v) if v is not None else None


def _rate(k, n):
    return k / n if isinstance(k, int) and isinstance(n, int) and n > 0 else None


def _round4(x):
    return None if x is None else round(x, 4)


def _outcome(v):
    """True/False/None for a recorded per-PR outcome (None means the evaluation errored)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    return None


def _pr_counts(pr_map):
    outcomes = [_outcome(v) for v in pr_map.values()] if isinstance(pr_map, dict) else []
    return {"n": len(outcomes), "passed": sum(1 for o in outcomes if o is True),
            "failed": sum(1 for o in outcomes if o is False), "errors": sum(1 for o in outcomes if o is None)}


def _sum_or_none(values):
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _scope_counts(library):
    counts = {s: 0 for s in SCOPES}
    counts["unknown"] = 0
    for skill in library if isinstance(library, list) else []:
        scope = skill.get("scope") if isinstance(skill, dict) else None
        counts[scope if scope in SCOPES else "unknown"] += 1
    return counts


def _library_size(library):
    return len(library) if isinstance(library, list) else None


def _apply_sim(lib, edits):
    """Apply recorded edits to a simulated library for the SkillOpt sensitivity analysis."""
    out = [dict(x) for x in lib]
    for e in edits or []:
        if not isinstance(e, dict):
            continue
        op = e.get("op")
        if op == "add":
            out.append({"id": e.get("id") or f"sim{len(out) + 1}", "scope": e.get("scope"), "text": e.get("text")})
        elif op == "replace":
            for x in out:
                if x.get("id") == e.get("id"):
                    x["text"] = e.get("text")
        elif op == "delete":
            out = [x for x in out if x.get("id") != e.get("id")]
    return out


def wilson_ci(k, n, z=1.959963984540054):
    """95% Wilson score interval for a binomial rate; None when there is no denominator."""
    if not isinstance(k, int) or not isinstance(n, int) or n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _color(cond, i):
    return COLORS.get(cond, FALLBACK_COLORS[i % len(FALLBACK_COLORS)])


# ---------- loading ----------
def read_jsonl(path):
    """(records, n_malformed, n_nonblank_lines). Missing/unreadable file -> ([], 0, 0); never raises on a bad line."""
    if not path.exists():
        return [], 0, 0
    recs, bad, n_lines = [], 0, 0
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return [], 0, 0
    for line in lines:
        if not line.strip():
            continue
        n_lines += 1
        try:
            obj = json.loads(line)
        except Exception:
            bad += 1
            continue
        if isinstance(obj, dict):
            recs.append(obj)
        else:
            bad += 1
    return recs, bad, n_lines


def split_records(recs):
    """-> (headers, rounds, terminal, n_unknown). Terminal = aborted/stopped markers."""
    headers, rounds, terminal, unknown = [], [], [], 0
    for r in recs:
        if "aborted" in r or "stopped" in r:
            terminal.append(r)
        elif "dev_ids" in r and "round" not in r:
            headers.append(r)
        elif "round" in r and any(k in r for k in ("accept", "dev", "edits")):
            if _int(r.get("round")) is None:
                unknown += 1
            else:
                rounds.append(r)
        else:
            unknown += 1
    return headers, rounds, terminal, unknown


def load_condition(cond, evo_dir):
    path = Path(evo_dir) / f"{cond}.jsonl"
    recs, malformed, n_lines = read_jsonl(path)
    headers, rounds, terminal, unknown = split_records(recs)
    by_round, duplicates = {}, 0
    for r in rounds:
        rn = _int(r.get("round"))
        if rn in by_round:
            duplicates += 1
        by_round[rn] = r  # latest record for a round number wins
    ordered = [by_round[k] for k in sorted(by_round)]
    # Drop stale higher-numbered rounds from an earlier run after a resume: the final library and
    # the freeze summary must come from the latest run lineage, not round 5 of an abandoned run.
    latest_id = str(headers[-1].get("run_id")) if headers else None
    if latest_id and any(str(r.get("run_id")) == latest_id for r in ordered):
        latest_rounds = [r for r in ordered if str(r.get("run_id")) == latest_id]
        max_latest = max(_int(r.get("round")) for r in latest_rounds)
        ordered = [r for r in ordered if _int(r.get("round")) <= max_latest]
    runs = {}
    for r in ordered:
        runs[str(r.get("run_id"))] = runs.get(str(r.get("run_id")), 0) + 1
    header = headers[-1] if headers else None
    return {
        "condition": cond, "path": path, "exists": path.exists(), "n_lines": n_lines,
        "n_records": len(recs), "malformed_lines": malformed, "n_headers": len(headers), "header": header,
        "n_headers_earlier": max(0, len(headers) - 1), "rounds": ordered,
        "terminal": terminal, "n_unknown": unknown, "n_duplicate_rounds": duplicates,
        "rounds_per_run": runs,
    }


# ---------- side summaries ----------
def side_stats(summ, pr_map=None):
    """Normalize an evolve.summarize dict (dev/sel_base/sel_new) plus an optional per-PR outcome map."""
    s = summ if isinstance(summ, dict) else {}
    n_prs, n_err, passed_prs = _int(s.get("n_prs")), _int(s.get("n_errors")), _int(s.get("passed_prs"))
    pr_ef, pr_cc = _num(s.get("pr_rate_errors_as_failed")), _num(s.get("pr_rate_complete_case"))
    if isinstance(pr_map, dict) and pr_map:
        pc = _pr_counts(pr_map)
        n_prs, n_err, passed_prs = pc["n"], pc["errors"], pc["passed"]
    elif isinstance(s.get("prs"), list):
        pc = _pr_counts({str(i): (p.get("passed") if isinstance(p, dict) else None)
                         for i, p in enumerate(s["prs"])})
        n_prs, n_err, passed_prs = pc["n"], pc["errors"], pc["passed"]
    if passed_prs is not None and n_prs:
        n_err = n_err or 0
        pr_ef = passed_prs / n_prs
        pr_cc = passed_prs / (n_prs - n_err) if n_prs - n_err > 0 else None
    total, passed = _int(s.get("n_comments")), _int(s.get("passed"))
    c_ef = _num(s.get("pass_rate_errors_as_failed"))
    c_cc = _num(s.get("pass_rate_excl_errors"))
    if c_cc is None:
        c_cc = _num(s.get("pass_rate_comments_excl_errors"))
    if c_ef is None:
        c_ef = _rate(passed, total)
    return {
        "n_prs": n_prs, "n_errors": n_err, "pr_passed": passed_prs,
        "pr_rate_errors_as_failed": _round4(pr_ef), "pr_rate_complete_case": _round4(pr_cc),
        "n_comments": total, "comments_passed": passed,
        "comment_rate_errors_as_failed": _round4(c_ef), "comment_rate_excl_errors": _round4(c_cc),
        "tokens": _int(s.get("tokens")), "prompt_tokens": _int(s.get("prompt_tokens")),
        "completion_tokens": _int(s.get("completion_tokens")), "duration_s": _num(s.get("duration_s")),
    }


def _edits_text(edits, n=90):
    parts = []
    for e in edits:
        if not isinstance(e, dict):
            continue
        op = e.get("op") or "edit"
        tag = e.get("scope") if op == "add" else e.get("id")
        head = f"{op}[{tag}]" if tag else str(op)
        text = _truncate(e.get("text"), n)
        parts.append(f"{head}: {text}" if text else head)
    return " | ".join(parts)


def round_row(cond, rec):
    """Flat CSV/JSON row for one round record."""
    dev_map = rec.get("dev_passed") if isinstance(rec.get("dev_passed"), dict) else None
    dev, selb, seln = side_stats(rec.get("dev"), dev_map), side_stats(rec.get("sel_base")), side_stats(rec.get("sel_new"))
    edits = [e for e in (rec.get("edits") or []) if isinstance(e, dict)]
    excluded = rec.get("excluded")
    gate_excluded = len(excluded) if isinstance(excluded, list) else _int(excluded)
    tokens = _sum_or_none([dev["tokens"], selb["tokens"], seln["tokens"]])
    duration = _sum_or_none([dev["duration_s"], selb["duration_s"], seln["duration_s"]])
    gated = rec.get("gated")
    if not isinstance(gated, bool):
        # evolve.run_round does not stamp `gated` on round records; infer it from what the round actually did
        if rec.get("sel_base") is not None or rec.get("sel_new") is not None or rec.get("gained") is not None:
            gated = True
        elif isinstance(rec.get("note"), str) and rec["note"].startswith("ungated"):
            gated = False
    return {
        "condition": cond,
        "round": _int(rec.get("round")),
        "run_id": rec.get("run_id"),
        "gated": gated if isinstance(gated, bool) else None,
        "edits_count": len(edits),
        "edits": _edits_text(edits),
        "accepted": rec.get("accept") if isinstance(rec.get("accept"), bool) else None,
        "gained": _int(rec.get("gained")),
        "regressed": _int(rec.get("regressed")),
        "net": _int(rec.get("net")),
        "gate_excluded": gate_excluded,
        "dev_n_prs": dev["n_prs"], "dev_n_errors": dev["n_errors"], "dev_pr_passed": dev["pr_passed"],
        "dev_pr_rate_errors_as_failed": dev["pr_rate_errors_as_failed"],
        "dev_pr_rate_complete_case": dev["pr_rate_complete_case"],
        "dev_comment_passed": dev["comments_passed"], "dev_comment_total": dev["n_comments"],
        "dev_comment_rate_errors_as_failed": dev["comment_rate_errors_as_failed"],
        "dev_comment_rate_excl_errors": dev["comment_rate_excl_errors"],
        "sel_base_pr_rate_errors_as_failed": selb["pr_rate_errors_as_failed"],
        "sel_base_pr_rate_complete_case": selb["pr_rate_complete_case"],
        "sel_new_pr_rate_errors_as_failed": seln["pr_rate_errors_as_failed"],
        "sel_new_pr_rate_complete_case": seln["pr_rate_complete_case"],
        "sel_base_comment_passed": selb["comments_passed"], "sel_base_comment_total": selb["n_comments"],
        "sel_base_comment_rate_errors_as_failed": selb["comment_rate_errors_as_failed"],
        "sel_base_comment_rate_excl_errors": selb["comment_rate_excl_errors"],
        "sel_new_comment_passed": seln["comments_passed"], "sel_new_comment_total": seln["n_comments"],
        "sel_new_comment_rate_errors_as_failed": seln["comment_rate_errors_as_failed"],
        "sel_new_comment_rate_excl_errors": seln["comment_rate_excl_errors"],
        "library_before": _library_size(rec.get("library_before")),
        "library_after": _library_size(rec.get("library_after")),
        "tokens": tokens, "duration_s": round(duration, 1) if duration is not None else None,
        "cost_usd": _round4(_num(rec.get("cost_usd"))),
        "note": rec.get("note") if isinstance(rec.get("note"), str) else None,
        "_sides": {"dev": dev, "sel_base": selb, "sel_new": seln},
    }


def round_detail(cond, rec, row):
    """Extra per-round fields kept only in the JSON summary (not the CSV)."""
    edits = []
    for e in (rec.get("edits") or []):
        if isinstance(e, dict):
            edits.append({"op": e.get("op"), "scope": e.get("scope"), "id": e.get("id"),
                          "text": e.get("text"), "text_short": _truncate(e.get("text"), 120)})
    sha = rec.get("library_sha256_after") or rec.get("library_sha256_before")
    return {
        "condition": cond, "round": row["round"], "run_id": rec.get("run_id"),
        "threshold": rec.get("threshold") if isinstance(rec.get("threshold"), dict) else None,
        "harness_version": rec.get("harness_version"), "model": rec.get("model"), "salt": rec.get("salt"),
        "edits_detail": edits,
        "library_scope_counts_before": _scope_counts(rec.get("library_before")),
        "library_scope_counts_after": _scope_counts(rec.get("library_after")),
        "library_sha256_after": sha,
        "dev_sides": row["_sides"],
    }


# ---------- aggregation ----------
def build_curves(rows):
    return {
        "rounds": [r["round"] for r in rows],
        "dev_comment_rate_errors_as_failed": [r["dev_comment_rate_errors_as_failed"] for r in rows],
        "dev_comment_rate_excl_errors": [r["dev_comment_rate_excl_errors"] for r in rows],
        "dev_pr_rate_errors_as_failed": [r["dev_pr_rate_errors_as_failed"] for r in rows],
        "sel_base_comment_rate_errors_as_failed": [r["sel_base_comment_rate_errors_as_failed"] for r in rows],
        "sel_new_comment_rate_errors_as_failed": [r["sel_new_comment_rate_errors_as_failed"] for r in rows],
        "sel_base_comment_rate_excl_errors": [r["sel_base_comment_rate_excl_errors"] for r in rows],
        "sel_new_comment_rate_excl_errors": [r["sel_new_comment_rate_excl_errors"] for r in rows],
        "library_size": [r["library_after"] for r in rows],
        "cumulative_accepted_edits": [],
        "cumulative_proposed_edits": [],
    }


def build_libraries(rows, records_by_round):
    """One snapshot per round (library_after) plus the round-0 / pre-first-round state."""
    snaps = []
    if rows and rows[0]["library_before"] is not None:
        first = rows[0]
        snaps.append({"round": max(0, (first["round"] or 1) - 1), "size": first["library_before"],
                      "scopes": _scope_counts(records_by_round[first["round"]].get("library_before")),
                      "source": "library_before of the first analysed round"})
    for r in rows:
        rec = records_by_round[r["round"]]
        snaps.append({"round": r["round"], "size": r["library_after"],
                      "scopes": _scope_counts(rec.get("library_after")),
                      "sha256": rec.get("library_sha256_after"),
                      "accepted": r["accepted"], "source": "library_after"})
    return snaps


def build_edit_lists(rows, records_by_round, cond):
    accepted, rejected = [], []
    for r in rows:
        rec = records_by_round[r["round"]]
        for e in (rec.get("edits") or []):
            if not isinstance(e, dict):
                continue
            item = {"condition": cond, "round": r["round"], "op": e.get("op"), "scope": e.get("scope"),
                    "id": e.get("id"), "text": e.get("text"), "text_short": _truncate(e.get("text"), 160),
                    "gained": r["gained"], "regressed": r["regressed"], "net": r["net"],
                    "library_size_after": r["library_after"], "run_id": r["run_id"]}
            if r["accepted"] is True:
                accepted.append(item)
            else:
                item["reason"] = "gate rejected" if r["gained"] is not None else (r["note"] or "not applied")
                rejected.append(item)
    return accepted, rejected


def condition_metrics(cond, rows, loaded):
    gated = [r for r in rows if r["gained"] is not None]
    accepted_gated = [r for r in gated if r["accepted"] is True]
    accepted_any = [r for r in rows if r["accepted"] is True]
    rejected = [r for r in rows if r["edits_count"] and r["accepted"] is not True]
    edits_accepted_gated = sum(r["edits_count"] for r in accepted_gated)
    edits_accepted_ungated = sum(r["edits_count"] for r in accepted_any if r["gained"] is None)
    # regressions are only defined on accepted gated rounds; gate totals cover every gated round (accepted or not)
    regressions = sum(r["regressed"] or 0 for r in accepted_gated)
    gains = sum(r["gained"] or 0 for r in accepted_gated)
    gated_gains = sum(r["gained"] or 0 for r in gated)
    gated_regressions = sum(r["regressed"] or 0 for r in gated)
    gap = []
    for r in rows:
        sel, dev = r["sel_new_comment_rate_errors_as_failed"], r["dev_comment_rate_errors_as_failed"]
        if sel is not None and dev is not None:
            gap.append({"round": r["round"], "sel_new_minus_dev": round(sel - dev, 4),
                        "sel_base_minus_dev": (round(r["sel_base_comment_rate_errors_as_failed"] - dev, 4)
                                               if r["sel_base_comment_rate_errors_as_failed"] is not None else None)})
    last = rows[-1] if rows else None
    final_library = next((r.get("library_after") for r in reversed(loaded["rounds"])
                          if isinstance(r.get("library_after"), list)), [])
    header = loaded.get("header") or {}
    raw_by_round = {_int(r.get("round")): r for r in loaded.get("rounds", [])}
    skillopt_rounds, sim_lib = [], []
    for r in rows:
        if r.get("gated") is not True or not r.get("edits_count"):
            continue
        gained, regressed = r.get("gained") or 0, r.get("regressed") or 0
        if gained >= 1 and regressed <= 1:  # SkillOpt rule, separate from the net gate
            edits = (raw_by_round.get(r.get("round")) or {}).get("edits") or []
            sim_lib = _apply_sim(sim_lib, edits)
            skillopt_rounds.append({"round": r.get("round"), "gained": gained, "regressed": regressed,
                                    "net": gained - regressed, "edits": len(edits),
                                    "simulated_library_size": len(sim_lib)})
    metrics = {
        "condition": cond,
        "log_path": str(loaded["path"]),
        "n_rounds": len(rows), "rounds": [r["round"] for r in rows],
        "first_round": rows[0]["round"] if rows else None, "last_round": last["round"] if last else None,
        "edits_proposed": sum(r["edits_count"] for r in rows),
        "edits_accepted": edits_accepted_gated + edits_accepted_ungated,
        "accepted_rounds": len(accepted_any), "rejected_rounds": len(rejected),
        "edits_accepted_gated": edits_accepted_gated, "edits_accepted_ungated": edits_accepted_ungated,
        "gated_rounds": len(gated), "ungated_rounds": len(rows) - len(gated),
        "gate_totals": {"gained": gated_gains, "regressed": gated_regressions,
                        "net": gated_gains - gated_regressions,
                        "excluded_prs": sum(r["gate_excluded"] or 0 for r in gated)} if gated else None,
        "regressions_per_accepted_edit": _round4(regressions / edits_accepted_gated) if edits_accepted_gated else None,
        "net_gain_per_accepted_edit": _round4((gains - regressions) / edits_accepted_gated) if edits_accepted_gated else None,
        "regressions_per_accepted_round": _round4(regressions / len(accepted_gated)) if accepted_gated else None,
        "skillopt_sensitivity": {
            "rule": "gained >= 1 and regressed <= 1 (not the net gate)",
            "accepted_rounds": skillopt_rounds,
            "n_accepted_rounds": len(skillopt_rounds),
            "simulated_library_size": len(sim_lib),
            "simulated_library": [{"id": x.get("id"), "scope": x.get("scope"), "text": x.get("text")} for x in sim_lib],
            "note": "Library chain simulated from the recorded edits; its pass rates are not re-evaluated",
        },
        "selection_to_dev_gap": {"per_round": gap,
                                 "mean": _round4(sum(g["sel_new_minus_dev"] for g in gap) / len(gap)) if gap else None,
                                 "final": gap[-1]["sel_new_minus_dev"] if gap else None},
        "totals": {"tokens": _sum_or_none([r["tokens"] for r in rows]),
                   "duration_s": _round4(_sum_or_none([r["duration_s"] for r in rows])),
                   "cost_usd": _round4(_sum_or_none([r["cost_usd"] for r in rows]))},
        "dev_errors_total": sum(r["dev_n_errors"] or 0 for r in rows),
        "library_size_start": next((r["library_before"] for r in rows if r["library_before"] is not None), None) if rows else None,
        "library_size_final": next((r["library_after"] for r in reversed(rows) if r["library_after"] is not None), None) if rows else None,
        "final_library_scopes": _scope_counts(final_library),
        "final_library": [{"id": s.get("id"), "scope": s.get("scope"), "text": s.get("text")}
                          for s in final_library if isinstance(s, dict)],
        "run_id": header.get("run_id"),
        "latest_header": {k: header.get(k) for k in ("run_id", "condition", "seed", "gated", "resumed_from", "salt",
                                                    "gate", "excluded")} if header else None,
        "n_headers": loaded["n_headers"], "n_headers_earlier": loaded["n_headers_earlier"],
        "rounds_per_run": loaded["rounds_per_run"],
        "terminal_records": [{k: t.get(k) for k in ("run_id", "aborted", "stopped", "round")} for t in loaded["terminal"]],
        "malformed_lines": loaded["malformed_lines"], "unknown_records": loaded["n_unknown"],
        "duplicate_rounds_collapsed": loaded["n_duplicate_rounds"],
    }
    if rows:
        last_side = rows[-1]["_sides"]["dev"]
        metrics["final_dev_comment_ci95"] = wilson_ci(last_side["comments_passed"], last_side["n_comments"])
        metrics["final_dev_pr_ci95"] = wilson_ci(last_side["pr_passed"], last_side["n_prs"])
    return metrics


def build_summary(loaded_all, conditions, out_dir):
    rows_all, details, libraries, accepted, rejected, metrics, curves = [], [], {}, [], [], {}, {}
    notes = []
    for cond in conditions:
        loaded = loaded_all.get(cond)
        if not loaded or not loaded["rounds"]:
            continue
        recs = {r["round"]: r for r in loaded["rounds"]}
        rows_full, rows, dets = [], [], []
        for rec in loaded["rounds"]:
            row = round_row(cond, rec)
            det = round_detail(cond, rec, row)
            dets.append(det)
            rows_full.append(row)
            rows.append({k: v for k, v in row.items() if not k.startswith("_")})
        # attach cumulative counts (needs the ordered rows)
        cum_acc, cum_prop = 0, 0
        curve = build_curves(rows)
        for r in rows:
            cum_prop += r["edits_count"]
            if r["accepted"] is True:
                cum_acc += r["edits_count"]
            curve["cumulative_proposed_edits"].append(cum_prop)
            curve["cumulative_accepted_edits"].append(cum_acc)
        curves[cond] = curve
        libraries[cond] = build_libraries(rows, recs)
        acc, rej = build_edit_lists(rows, recs, cond)
        accepted += acc
        rejected += rej
        metrics[cond] = condition_metrics(cond, rows_full, loaded)
        rows_all += rows
        details += dets
        if loaded["n_headers_earlier"]:
            notes.append(f"{cond}: {loaded['n_headers_earlier']} earlier run header(s) in the log; only the last one is "
                         f"reported as run metadata.")
        if loaded["malformed_lines"]:
            notes.append(f"{cond}: {loaded['malformed_lines']} malformed line(s) skipped.")
        if loaded["n_unknown"]:
            notes.append(f"{cond}: {loaded['n_unknown']} line(s) were neither header, round nor aborted/stopped records.")
        if loaded["n_duplicate_rounds"]:
            notes.append(f"{cond}: {loaded['n_duplicate_rounds']} duplicate round record(s) collapsed to the latest.")
        hdr_run = (loaded["header"] or {}).get("run_id")
        stray = sum(1 for r in rows if hdr_run and r["run_id"] and r["run_id"] != hdr_run)
        if stray:
            notes.append(f"{cond}: {stray} round(s) come from an earlier run_id than the latest header (resume); they are "
                         f"kept so the curve is continuous.")
        missing_sel = [r["round"] for r in rows if r["gained"] is not None and r["sel_new_comment_rate_errors_as_failed"] is None]
        if missing_sel:
            notes.append(f"{cond}: gated round(s) {missing_sel} have no selection comment totals recorded.")

    summary = {
        "generated_by": "analyze_evolution.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "out_dir": str(Path(out_dir)),
        "conditions": [c for c in conditions if c in metrics],
        "sources": {c: {"path": str(loaded_all[c]["path"]), "exists": loaded_all[c]["exists"],
                        "n_lines": loaded_all[c]["n_lines"], "n_records": loaded_all[c]["n_records"],
                        "n_rounds": len(loaded_all[c]["rounds"]),
                        "malformed_lines": loaded_all[c]["malformed_lines"],
                        "unknown_records": loaded_all[c]["n_unknown"]}
                    for c in conditions if c in loaded_all},
        "rounds": details,
        "table": rows_all,
        "curves": curves,
        "libraries": libraries,
        "accepted_edits": accepted,
        "rejected_edits": rejected,
        "conditions_summary": metrics,
        "notes": notes,
        "assumptions": ASSUMPTIONS,
        "figures": [str(Path(out_dir) / n) for n in OUTPUT_NAMES[2:]],
        "csv_columns": ROW_FIELDS,
    }
    return summary


# ---------- figures ----------
def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(alpha=0.25, lw=0.6)
    ax.set_xlabel("evolution round")


def _legend(ax, size=7):
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(fontsize=size, loc="best")


def _series(ax, rounds, ys, **kw):
    pts = [(x, y) for x, y in zip(rounds, ys) if y is not None]
    if pts:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], **kw)


def make_figures(summary, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    out_dir = Path(out_dir)
    conds = summary["conditions"]
    colors = {c: _color(c, i) for i, c in enumerate(conds)}
    wrote = []

    fig, axes = plt.subplots(2, 2, figsize=(11, 7.6))
    ax = axes[0, 0]
    for c in conds:
        cv = summary["curves"][c]
        _series(ax, cv["rounds"], cv["dev_comment_rate_errors_as_failed"], color=colors[c], marker="o", lw=1.8,
                label=f"{c} (errors as failed)")
        _series(ax, cv["rounds"], cv["dev_comment_rate_excl_errors"], color=colors[c], marker="o", lw=1.0,
                ls="--", alpha=0.7, label=f"{c} (complete case)")
    ax.set_title("Development: comment-level pass rate", fontsize=10)
    ax.set_ylabel("pass rate")
    ax.set_ylim(0, 1)
    _style(ax)
    _legend(ax, 7)

    ax = axes[0, 1]
    for c in conds:
        cv = summary["curves"][c]
        _series(ax, cv["rounds"], cv["sel_base_comment_rate_errors_as_failed"], color=colors[c], marker="x",
                lw=1.0, ls=":", alpha=0.8, label=f"{c} baseline")
        _series(ax, cv["rounds"], cv["sel_new_comment_rate_errors_as_failed"], color=colors[c], marker="s",
                lw=1.8, label=f"{c} candidate")
    ax.set_title("Selection: comment-level pass rate (gated rounds)", fontsize=10)
    ax.set_ylabel("pass rate")
    ax.set_ylim(0, 1)
    _style(ax)
    _legend(ax, 7)

    ax = axes[1, 0]
    for c in conds:
        libs = summary["libraries"][c]
        xs = [s["round"] for s in libs]
        ys = [s["size"] for s in libs]
        pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=colors[c], marker="o", lw=1.8,
                    drawstyle="steps-post", label=c)
    ax.set_title("Library size (skills)", fontsize=10)
    ax.set_ylabel("skills")
    _style(ax)
    _legend(ax, 8)

    ax = axes[1, 1]
    for c in conds:
        cv = summary["curves"][c]
        _series(ax, cv["rounds"], cv["cumulative_accepted_edits"], color=colors[c], marker="o", lw=1.8,
                label=f"{c} accepted")
        _series(ax, cv["rounds"], cv["cumulative_proposed_edits"], color=colors[c], marker=".", lw=1.0,
                ls="--", alpha=0.6, label=f"{c} proposed")
    ax.set_title("Cumulative edits", fontsize=10)
    ax.set_ylabel("edits")
    _style(ax)
    _legend(ax, 7)
    for a in list(axes.flat):
        a.set_xlabel("evolution round")
        if a.get_visible():
            a.xaxis.set_major_locator(MaxNLocator(integer=True))
    fig.tight_layout()
    fig.savefig(out_dir / "fig_evolution.png", dpi=180)
    plt.close(fig)
    wrote.append("fig_evolution.png")

    gated = {c: [r for r in summary["table"] if r["condition"] == c and r["gained"] is not None] for c in conds}
    gated = {c: rs for c, rs in gated.items() if rs}
    if gated:
        fig, axes_arr = plt.subplots(1, len(gated), figsize=(5.6 * len(gated), 3.8), squeeze=False)
        for ax, (c, rs) in zip(axes_arr[0], gated.items()):
            xs = [r["round"] for r in rs]
            gained = [r["gained"] or 0 for r in rs]
            regressed = [-(r["regressed"] or 0) for r in rs]
            width = 0.36
            ax.bar([x - width / 2 for x in xs], gained, width=width, color="#2e8b57", label="gained")
            ax.bar([x + width / 2 for x in xs], regressed, width=width, color="#c0392b", label="regressed")
            ax.plot(xs, [r["net"] or 0 for r in rs], color="black", marker="D", lw=1.4, ms=4, label="net")
            gate = (summary["conditions_summary"][c].get("latest_header") or {}).get("gate") or {}
            min_gain, max_reg = _int(gate.get("min_gain")), _int(gate.get("max_regress"))
            if min_gain is not None:
                ax.axhline(min_gain, color="#555555", ls="--", lw=0.9, label=f"min net gain (+{min_gain})")
            if max_reg is not None:
                ax.axhline(-max_reg, color="#555555", ls="--", lw=0.9, label=f"max regressions (-{max_reg})")
            ax.axhline(0, color="black", lw=0.8)
            ax.set_xticks(xs)
            ax.set_xlim(min(xs) - 0.7, max(xs) + 0.7)
            ax.set_xlabel("evolution round")
            ax.set_title(f"{c}: selection gate per round", fontsize=10)
            ax.set_ylabel("selection PRs (regressed shown negative)")
            ax.set_ylim(min(-1, min(regressed + [-1])) - 0.6, max(1, max(gained + [1])) + 0.8)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", alpha=0.25, lw=0.6)
            _legend(ax, 8)
        fig.tight_layout()
    else:
        fig, ax = plt.subplots(figsize=(6.4, 3.2))
        ax.text(0.5, 0.5, "No gated selection rounds recorded\n(ungated rounds have gained/regressed = null)",
                ha="center", va="center", fontsize=10, transform=ax.transAxes)
        ax.set_axis_off()
    fig.savefig(out_dir / "fig_gate.png", dpi=180)
    plt.close(fig)
    wrote.append("fig_gate.png")
    return wrote


# ---------- output ----------
def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        text = f"{v:.4f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-") else "0"
    return v


def write_outputs(summary, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evolution_summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    with (out_dir / "evolution_table.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in summary["table"]:
            w.writerow({k: _csv_value(row.get(k)) for k in ROW_FIELDS})
    return ["evolution_summary.json", "evolution_table.csv"]


def parse_conditions(text):
    names, seen = [], set()
    for raw in str(text or "").split(","):
        name = raw.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        raise SystemExit("--conditions needs at least one condition name")
    unknown = [n for n in names if n not in CONDITIONS]
    if unknown:
        raise SystemExit(f"unknown condition(s) {unknown}; choose from {list(CONDITIONS)}")
    return names


def main(argv=None):
    ap = argparse.ArgumentParser(description="Evolution tables and figures from results/evolution/<condition>.jsonl")
    ap.add_argument("--conditions", default=",".join(CONDITIONS),
                    help="comma-separated conditions to analyse (default: global,scoped)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT), help="output directory (default: results/analysis)")
    ap.add_argument("--evo-dir", default=str(EVO_DIR), help="directory holding <condition>.jsonl (default: results/evolution)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be read/written, write nothing")
    args = ap.parse_args(argv)

    conditions = parse_conditions(args.conditions)
    evo_dir, out_dir = Path(args.evo_dir), Path(args.out_dir)
    loaded_all = {c: load_condition(c, evo_dir) for c in conditions}

    print("analyze_evolution: evolution round logs")
    for c in conditions:
        ld = loaded_all[c]
        if not ld["exists"]:
            state = "missing"
        else:
            state = f"{ld['n_lines']} line(s), {ld['n_records']} record(s), {len(ld['rounds'])} round(s)"
            if ld["malformed_lines"]:
                state += f", {ld['malformed_lines']} malformed"
            if ld["n_unknown"]:
                state += f", {ld['n_unknown']} unrecognised"
            if ld["n_duplicate_rounds"]:
                state += f", {ld['n_duplicate_rounds']} duplicate round(s)"
        print(f"  {c:7s} {ld['path']}  [{state}]")

    if not any(loaded_all[c]["rounds"] for c in conditions):
        print("no evolution round records yet: nothing to analyse. Run evolve.py evolve for a condition first "
              f"(looked in {evo_dir}/{{{','.join(conditions)}}}.jsonl).")
        return 0

    summary = build_summary(loaded_all, conditions, out_dir)
    plans = [str(out_dir / n) for n in OUTPUT_NAMES]

    if args.dry_run:
        print("[dry-run] nothing written. Would read:")
        for c in summary["conditions"]:
            src = summary["sources"][c]
            print(f"  {src['path']} -> {src['n_rounds']} round(s)")
        print("[dry-run] would write:")
        for p in plans:
            print(f"  {p}")
        print(f"[dry-run] table rows: {len(summary['table'])}; conditions: {summary['conditions']}; "
              f"accepted edits: {len(summary['accepted_edits'])}, rejected edits: {len(summary['rejected_edits'])}")
        for c in summary["conditions"]:
            m = summary["conditions_summary"][c]
            last = summary["curves"][c]["dev_comment_rate_errors_as_failed"][-1]
            last_s = "n/a" if last is None else f"{100 * last:.1f}%"
            print(f"  {c}: rounds={m['n_rounds']} accepted_rounds={m['accepted_rounds']} "
                  f"edits_accepted={m['edits_accepted']} final_library={m['library_size_final']} "
                  f"dev_rate_last={last_s}")
        return 0

    write_outputs(summary, out_dir)
    try:
        wrote = make_figures(summary, out_dir)
    except Exception as e:  # figures are secondary; the JSON/CSV numbers must still be written
        print(f"warning: figure generation failed ({type(e).__name__}: {e})", file=sys.stderr)
        wrote = []
        summary["figures"] = []
        summary["notes"].append(f"figures not written: {type(e).__name__}: {e}")
        (out_dir / "evolution_summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    print("wrote: " + ", ".join(str(out_dir / n) for n in ["evolution_summary.json", "evolution_table.csv"] + wrote))
    for c in summary["conditions"]:
        m = summary["conditions_summary"][c]
        cv = summary["curves"][c]
        last = cv["dev_comment_rate_errors_as_failed"][-1]
        last_s = "n/a" if last is None else f"{100 * last:.1f}%"
        print(f"  {c}: {m['n_rounds']} round(s), {m['accepted_rounds']} accepted / {m['rejected_rounds']} rejected, "
              f"library {m['library_size_start']} -> {m['library_size_final']}, dev pass (last) {last_s}, "
              f"gap sel-dev (mean) {m['selection_to_dev_gap']['mean']}")
    for n in summary["notes"]:
        print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
