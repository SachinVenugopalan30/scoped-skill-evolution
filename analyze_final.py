"""Final locked-test analysis for the scoped-skill-evolution study.

Input (test partition): the scored outputs `results/test_<condition>.json` written by
`evolve.py score --unlock-test`. Those files are the *only* test-partition artifacts read here; no
test PR, diff, comment text or gold outcome is ever loaded. Everything else is metadata that was
produced before the freeze (`results/static_selection.json`, the evolution round logs, or an
aggregated `results/analysis/evolution_summary.json` if one exists).

Output: `results/analysis/final_summary.json` and `results/analysis/final_table.csv`.

Statistics follow PROTOCOL.md section 13:
  * repository-clustered bootstrap (10,000 resamples) for rates and for paired differences
    (reuses analyze.bootstrap_ci for the per-condition 95% CI);
  * paired sign-flip permutation across repositories (10,000 permutations) for scoped vs global
    (reuses analyze.paired_permutation);
  * McNemar on paired comment outcomes (exact binomial for < 25 discordant pairs, otherwise the
    continuity-corrected chi-square; scipy is not a dependency, so the chi-square tail uses math.erfc).

Missing test files are not an error: the script prints what is missing and exits 0 without writing.
`--dry-run` computes and prints everything but writes nothing.
"""
import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import analyze

ROOT = Path(__file__).parent
R = ROOT / "results"
AN = R / "analysis"

MAX_SKILLS = 6            # evolve.MAX_SKILLS; imported as a number to avoid pulling in litellm
EXACT_MAX_DISCORDANT = 25  # protocol: exact binomial for small discordant counts, else chi-square
PRIMARY = ("scoped", "global")
PREFERRED_ORDER = ["static", "global", "scoped", "global-rich", "scoped-rich"]

CSV_COLUMNS = [
    "row_type", "condition", "partition", "n_prs", "n_comments", "n_repos", "n_errors", "error_rate",
    "passed_comments", "pass_rate", "ci_lo", "ci_hi",
    "pr_all_pass_n", "pr_all_pass_rate", "pr_all_pass_rate_excl_errors", "pass_rate_excl_errors",
    "tokens", "tokens_per_pr", "tokens_per_comment", "tokens_per_passing_review",
    "tokens_per_passing_comment", "duration_s", "seconds_per_passing_review",
    "mean_skills_per_review", "reviews_with_skills", "cap6_reviews",
    "scored_pass_rate", "selection_rate", "transfer_gap", "transfer_gap_pp",
    "accepted_edits", "regressions_per_accepted_edit", "net_gain_per_accepted_edit",
    "library_size", "library_sha256", "salt", "code_sha",
    # contrast-only columns
    "contrast_vs", "diff_rate", "diff_pp", "diff_ci_lo", "diff_ci_hi",
    "mcnemar_a_only", "mcnemar_b_only", "mcnemar_discordant", "mcnemar_method", "mcnemar_p",
    "permutation_p", "prs_gained", "prs_lost",
]


# ---------------------------------------------------------------- loading
def _discover(results_dir=None):
    """All scored test outputs, in a stable order: static, global, scoped, then the rest."""
    paths = sorted(Path(results_dir or R).glob("test_*.json"))
    found = {}
    for p in paths:
        cond = p.stem[len("test_"):]
        if not cond:
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            found[cond] = p  # _load will report the malformed file
            continue
        if d.get("partition") != "test":
            continue  # never put a dev/selection copy into the locked-test table
        found[cond] = p
    order = [c for c in PREFERRED_ORDER if c in found]
    order += sorted(c for c in found if c not in PREFERRED_ORDER)
    return [(c, found[c]) for c in order]


def _load(path):
    d = json.loads(path.read_text())
    if not isinstance(d, dict):
        raise ValueError(f"{path.name}: expected a JSON object")
    return d


def _comment_rows(d):
    """Normalized one-row-per-reference-comment table: {instance_id, repo, comment_index, passed, error, pr_type}.

    Prefers the `comments` field written by `evolve.score_library` (errors-as-failed is already imputed there).
    Falls back to `prs[].per_comment`, which cannot reconstruct the comments of an errored PR; that case is
    flagged in `source` so the denominator is not silently shrunk.
    """
    prs = [p for p in (d.get("prs") or []) if isinstance(p, dict) and p.get("instance_id")]
    pr_type = {p["instance_id"]: (p.get("pr_type") or "other") for p in prs}
    repo_of = {p["instance_id"]: (p.get("repo") or "") for p in prs}
    rows, source = d.get("comments"), "comments"
    if not isinstance(rows, list) or not rows:
        source = "prs.per_comment"
        rows = []
        for p in prs:
            if p.get("passed") is None:      # errored PR: its comments are not recoverable from per_comment
                continue
            for c in p.get("per_comment") or []:
                if not isinstance(c, dict):
                    continue
                rows.append({"instance_id": p["instance_id"], "repo": p.get("repo") or "",
                             "comment_index": c.get("comment_index"), "passed": c.get("passed") is True,
                             "error": False})
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("instance_id") or r.get("comment_index") is None:
            continue
        iid = r["instance_id"]
        out.append({"instance_id": iid, "repo": r.get("repo") or repo_of.get(iid, ""),
                    "comment_index": r["comment_index"], "passed": r.get("passed") is True,
                    "error": bool(r.get("error")), "pr_type": pr_type.get(iid) or "other"})
    return out, source


# ---------------------------------------------------------------- statistics
def _rate(rows):
    return (sum(1 for r in rows if r["passed"]) / len(rows)) if rows else None


def _bootstrap(rows, n, seed):
    """analyze.bootstrap_ci on comment rows; degenerate for 0 comments or a single repository."""
    if not rows:
        return {"rate": None, "lo": None, "hi": None, "n_repos": 0}
    ci = analyze.bootstrap_ci(rows, n=n, seed=seed)
    return {"rate": ci["rate"], "lo": ci["lo"], "hi": ci["hi"],
            "n_repos": len({r["repo"] for r in rows})}


def _paired_diff_ci(a_rows, b_rows, n=10000, seed=0):
    """Repository-clustered bootstrap CI of (rate_a - rate_b) on paired comments.

    Pairing key is (instance_id, comment_index); whole repositories are resampled with replacement, so
    comments of one repository move together, exactly like the unpaired CI.
    """
    bk = {(r["instance_id"], r["comment_index"]): bool(r["passed"]) for r in b_rows}
    per_repo = {}
    for r in a_rows:
        k = (r["instance_id"], r["comment_index"])
        if k not in bk:
            continue
        slot = per_repo.setdefault(r["repo"], [0, 0])
        slot[0] += (1 if r["passed"] else 0) - (1 if bk[k] else 0)
        slot[1] += 1
    if not per_repo:
        return {"diff": None, "lo": None, "hi": None, "n_pairs": 0, "n_repos": 0}
    repos = sorted(per_repo)
    d = np.array([per_repo[x][0] for x in repos], dtype=float)
    m = np.array([per_repo[x][1] for x in repos], dtype=float)
    idx = np.random.default_rng(seed).integers(0, len(repos), (n, len(repos)))
    diffs = d[idx].sum(1) / m[idx].sum(1)
    return {"diff": float(d.sum() / m.sum()), "lo": float(np.percentile(diffs, 2.5)),
            "hi": float(np.percentile(diffs, 97.5)), "n_pairs": int(m.sum()), "n_repos": len(repos)}


def _mcnemar(a_rows, b_rows):
    """McNemar on paired comments. Exact two-sided binomial for few discordant pairs, else chi-square (df=1)."""
    bk = {(r["instance_id"], r["comment_index"]): bool(r["passed"]) for r in b_rows}
    a_only = b_only = 0
    for r in a_rows:
        k = (r["instance_id"], r["comment_index"])
        if k not in bk:
            continue
        if r["passed"] and not bk[k]:
            a_only += 1
        elif bk[k] and not r["passed"]:
            b_only += 1
    disc = a_only + b_only
    if disc == 0:
        return {"a_only": 0, "b_only": 0, "discordant": 0, "method": "none", "p": 1.0}
    if disc < EXACT_MAX_DISCORDANT:
        tail = sum(math.comb(disc, i) for i in range(min(a_only, b_only) + 1)) / (2 ** disc)
        p = min(1.0, 2.0 * tail)
        method = "exact_binomial"
    else:
        chi2 = (abs(a_only - b_only) - 1.0) ** 2 / disc
        p = math.erfc(math.sqrt(max(chi2, 0.0) / 2.0))   # chi-square df=1 upper tail
        method = "chi2_continuity_corrected"
    return {"a_only": a_only, "b_only": b_only, "discordant": disc, "method": method, "p": float(p)}


def _pr_level(prs):
    """PR-level all-comments-pass rollup, errors counted as failed (and a complete-case variant)."""
    n = len(prs)
    err = sum(1 for p in prs if p.get("passed") is None)
    ok = sum(1 for p in prs if p.get("passed") is True)
    clean = n - err
    return {"n_prs": n, "n_errors": err, "n_all_pass": ok,
            "rate_errors_as_failed": (ok / n) if n else None,
            "rate_excl_errors": (ok / clean) if clean else None}


# ---------------------------------------------------------------- selection + evolution metadata
def _as_rounds(value):
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, dict):
        if "accept" in value or "regressed" in value or "gained" in value:
            return [value]
        for key in ("rounds", "records", "history", "evolution"):
            if key in value:
                got = _as_rounds(value[key])
                if got:
                    return got
    return []


def _rounds_for(obj, cond):
    """Find the round records of one condition in an evolution summary of unknown shape.

    Accepted shapes: {cond: [...]}, {cond: {"rounds": [...]}}, {"conditions": {cond: {...}}},
    {"rounds"/"table": [{...,"condition": cond}, ...]} (analyze_evolution.py), or a flat list of records
    carrying a "condition" field. A per-round "table" is preferred over the summary's "rounds" details
    because only the table rows carry the flattened selection rates.
    """
    if isinstance(obj, dict):
        candidates = (obj.get("table"), obj.get("rounds"), obj, obj.get("conditions"),
                      obj.get("evolution"), obj.get("records"), obj.get("history"))
        for holder in candidates:
            if isinstance(holder, dict) and cond in holder:
                got = _as_rounds(holder[cond])
                if got:
                    return got
            if isinstance(holder, list):
                got = [r for r in holder if isinstance(r, dict) and r.get("condition") == cond]
                if got:
                    return got
    found = []

    def walk(node):
        if isinstance(node, dict):
            if any(k in node for k in ("accept", "accepted", "edits", "edits_detail", "edits_count")):
                found.append(node)
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    # Strict match only: an untagged record could belong to another condition (results must not leak
    # between arms), so records without a condition field are ignored here and the jsonl log is used instead.
    return [r for r in found if r.get("condition") == cond]


def _accept_of(r):
    """evolve.run_round stores `accept`; analyze_evolution.py's table stores `accepted`."""
    for k in ("accept", "accepted"):
        if isinstance(r.get(k), bool):
            return r[k]
    return None


def _edits_count(r):
    for k in ("edits", "edits_detail"):
        e = r.get(k)
        if isinstance(e, list):
            return len(e)
    n = r.get("edits_count")
    return int(n) if isinstance(n, int) and not isinstance(n, bool) else 0


def _num_or_none(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _round_selection_rate(r):
    """(rate, side) for one round: the candidate's rate if accepted, else the baseline's.

    Handles nested sel_new/sel_base summaries (evolve.run_round), flattened table columns
    (sel_new_comment_rate_errors_as_failed) and the `_sides`/`dev_sides` blocks.
    """
    accept = _accept_of(r)
    sides = r.get("_sides") if isinstance(r.get("_sides"), dict) else None
    sides = sides or (r.get("dev_sides") if isinstance(r.get("dev_sides"), dict) else {})
    order = ("sel_new", "sel_base") if accept is not False else ("sel_base", "sel_new")
    for side in order:
        for holder in (r, r.get(side), sides.get(side)):
            if not isinstance(holder, dict):
                continue
            for k in (f"{side}_comment_rate_errors_as_failed", "comment_rate_errors_as_failed",
                      f"{side}_pr_rate_errors_as_failed", "pr_rate_errors_as_failed",
                      "pass_rate_errors_as_failed", "pass_rate"):
                v = _num_or_none(holder.get(k))
                if v is not None:
                    return v, side
    return None, None


def _selection_rate_from_rounds(rounds):
    """Selection pass rate of the *final* library: the candidate's rate if accepted, else the baseline's."""
    for r in reversed(rounds):
        rate, _side = _round_selection_rate(r)
        if rate is not None:
            return rate, "evolution_rounds"
    return None, None


def _evolution_metrics(rounds, source):
    if not rounds:
        return {"source": source, "rounds_evaluated": 0, "accepted_edits": 0, "accepted_edits_gated": 0,
                "rejected_edits": 0, "regressions": None, "gains": None,
                "regressions_per_accepted_edit": None, "net_gain_per_accepted_edit": None,
                "selection_rate": None, "selection_rate_source": None}
    accepted = [r for r in rounds if _accept_of(r) is True]
    rejected = [r for r in rounds if _accept_of(r) is False and _edits_count(r)]
    # Ungated accepted rounds record gained/regressed as None, so the regression ratio uses gated accepted
    # rounds only (the protocol's regressions-per-accepted-edit denominator).
    accepted_gated = [r for r in accepted if _num_or_none(r.get("regressed")) is not None]
    accepted_edits = sum(_edits_count(r) for r in accepted)
    accepted_edits_gated = sum(_edits_count(r) for r in accepted_gated)
    regressions = sum(int(_num_or_none(r.get("regressed"))) for r in accepted_gated) if accepted_gated else None
    gains = sum(int(_num_or_none(r.get("gained"))) for r in accepted_gated) if accepted_gated else None
    sel, sel_src = _selection_rate_from_rounds(rounds)
    return {"source": source, "rounds_evaluated": len(rounds), "accepted_edits": accepted_edits,
            "accepted_edits_gated": accepted_edits_gated,
            "rejected_edits": sum(_edits_count(r) for r in rejected),
            "regressions": regressions, "gains": gains,
            "regressions_per_accepted_edit": (regressions / accepted_edits_gated)
            if (accepted_edits_gated and regressions is not None) else None,
            "net_gain_per_accepted_edit": ((gains - regressions) / accepted_edits_gated)
            if (accepted_edits_gated and gains is not None and regressions is not None) else None,
            "selection_rate": sel, "selection_rate_source": sel_src}


def _evolution_jsonl(cond, results_dir=None):
    """Fallback: read the condition's round log (pre-freeze metadata, never test data)."""
    path = Path(results_dir or R) / f"evolution/{cond}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and ("accept" in rec or "edits" in rec):
            out.append(rec)
    return out


def _evolution_from_summary(cond, summary):
    """Round-derived metrics, overridden by analyze_evolution.py's `conditions_summary` aggregates when present."""
    rounds = _rounds_for(summary, cond)
    agg = None
    if isinstance(summary, dict) and isinstance(summary.get("conditions_summary"), dict):
        agg = summary["conditions_summary"].get(cond)
    if not rounds and not isinstance(agg, dict):
        return None
    base = _evolution_metrics(rounds, "results/analysis/evolution_summary.json")
    if isinstance(agg, dict):
        base["rounds_evaluated"] = agg.get("n_rounds", base["rounds_evaluated"])
        if agg.get("edits_accepted") is not None:
            base["accepted_edits"] = agg["edits_accepted"]
        if agg.get("edits_accepted_gated") is not None:
            base["accepted_edits_gated"] = agg["edits_accepted_gated"]
        if agg.get("rejected_rounds") is not None:
            base["rejected_rounds"] = agg["rejected_rounds"]
        base["regressions_per_accepted_edit"] = agg.get("regressions_per_accepted_edit",
                                                        base["regressions_per_accepted_edit"])
        base["net_gain_per_accepted_edit"] = agg.get("net_gain_per_accepted_edit",
                                                     base["net_gain_per_accepted_edit"])
        totals = agg.get("gate_totals") if isinstance(agg.get("gate_totals"), dict) else {}
        if totals:
            base["gains"], base["regressions"] = totals.get("gained"), totals.get("regressed")
    return base


def _evolution_metadata(cond, summary, results_dir=None):
    if summary is not None:
        got = _evolution_from_summary(cond, summary)
        if got is not None:
            return got
    rounds = _evolution_jsonl(cond, results_dir)
    if rounds:
        return _evolution_metrics(rounds, f"results/evolution/{cond}.jsonl")
    return _evolution_metrics([], None)


def _static_selection_rate(results_dir=None):
    path = Path(results_dir or R) / "static_selection.json"
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
    except Exception:
        return None
    if d.get("salt") not in (None, "static-v1"):
        return None  # a calibration draw overwrote the baseline; do not report it as the static reference
    return (d.get("summary") or {}).get("pass_rate_errors_as_failed")


# ---------------------------------------------------------------- per-condition metrics
def _condition_metrics(cond, path, n_boot, seed, evo_summary, results_dir=None):
    d = _load(path)
    rows, comments_source = _comment_rows(d)
    prs = [p for p in (d.get("prs") or []) if isinstance(p, dict)]
    summ = d.get("summary") or {}
    prmet = _pr_level(prs)

    ci = _bootstrap(rows, n_boot, seed)
    passed = sum(1 for r in rows if r["passed"])
    n_comments = len(rows)
    clean_rows = [r for r in rows if not r["error"]]

    by_type = {}
    for r in rows:
        st = by_type.setdefault(r["pr_type"], {"n_prs": 0, "n_comments": 0, "passed": 0})
        st["n_comments"] += 1
        st["passed"] += 1 if r["passed"] else 0
    for p in prs:
        st = by_type.setdefault(p.get("pr_type") or "other", {"n_prs": 0, "n_comments": 0, "passed": 0})
        st["n_prs"] += 1
    for st in by_type.values():
        st["rate"] = st["passed"] / st["n_comments"] if st["n_comments"] else None
        st["pr_all_pass_rate"] = None  # filled below
    for p in prs:
        st = by_type[p.get("pr_type") or "other"]
        st.setdefault("_ok", 0)
        st["_ok"] += 1 if p.get("passed") is True else 0
    for st in by_type.values():
        st["pr_all_pass_rate"] = (st.pop("_ok", 0) / st["n_prs"]) if st["n_prs"] else None

    usage_tokens = summ.get("tokens")
    if usage_tokens is None:
        usage_tokens = sum(int((p.get("usage") or {}).get("total_tokens") or 0) for p in prs)
    duration = summ.get("duration_s")
    if not duration:
        duration = sum(float(p.get("duration_s") or 0) for p in prs)
    duration = float(duration or 0)

    tokens = int(usage_tokens or 0)
    cost = {
        "tokens": tokens, "prompt_tokens": summ.get("prompt_tokens"),
        "completion_tokens": summ.get("completion_tokens"), "duration_s": duration,
        "duration_recorded": duration > 0,
        "tokens_per_pr": (tokens / len(prs)) if prs else None,
        "tokens_per_comment": (tokens / n_comments) if n_comments else None,
        "tokens_per_passing_review": (tokens / prmet["n_all_pass"]) if prmet["n_all_pass"] else None,
        "tokens_per_passing_comment": (tokens / passed) if passed else None,
        "seconds_per_pr": (duration / len(prs)) if prs else None,
        "seconds_per_passing_review": (duration / prmet["n_all_pass"])
        if (prmet["n_all_pass"] and duration > 0) else None,
        "cost_usd": d.get("cost_usd"),
    }

    # Retrieval diagnostics from the test PRs' skill_ids (ids only; no comment or diff content).
    lib = d.get("library") or []
    scope_of = {s.get("id"): (s.get("scope") or "?") for s in lib if isinstance(s, dict)}
    counts = [len(p.get("skill_ids") or []) for p in prs]
    per_skill = Counter(sid for p in prs for sid in (p.get("skill_ids") or []))
    scope_mix = Counter(scope_of.get(sid, "?") for sid in per_skill.elements())
    retrieval = {
        "library_size": len(lib),
        "library_scopes": dict(Counter((s.get("scope") or "?") for s in lib if isinstance(s, dict))),
        "reviews": len(prs), "reviews_with_skills": sum(1 for c in counts if c > 0),
        "mean_skills_per_review": (sum(counts) / len(counts)) if counts else 0.0,
        "max_skills_per_review": max(counts) if counts else 0,
        "cap6_reviews": sum(1 for c in counts if c >= MAX_SKILLS),
        "count_histogram": {str(k): counts.count(k) for k in range(0, max(counts + [0]) + 1)},
        "retrievals_total": int(sum(counts)), "per_skill": dict(sorted(per_skill.items())),
        "retrievals_by_scope": dict(scope_mix),
    }

    if cond == "static":
        sel_rate = _static_selection_rate(results_dir)
        evo = {"source": "results/static_selection.json", "accepted_edits": 0, "rounds_evaluated": 0,
               "regressions_per_accepted_edit": None, "net_gain_per_accepted_edit": None,
               "rejected_edits": 0, "selection_rate": sel_rate,
               "selection_rate_source": "static_selection" if sel_rate is not None else None}
    else:
        evo = _evolution_metadata(cond, evo_summary, results_dir)
        sel_rate = evo.get("selection_rate")

    transfer = {
        "selection_rate": sel_rate,
        "test_rate": ci["rate"],
        "gap": (ci["rate"] - sel_rate) if (ci["rate"] is not None and sel_rate is not None) else None,
        "gap_pp": (100.0 * (ci["rate"] - sel_rate))
        if (ci["rate"] is not None and sel_rate is not None) else None,
        "note": "transfer diagnostic only; selection is never reported as held-out performance",
    }

    err_rows = sum(1 for r in rows if r["error"])
    metrics = {
        "condition": cond,
        "partition": d.get("partition"),
        "n_prs": len(prs), "n_prs_requested": d.get("n_requested"), "n_prs_done": d.get("n_done", len(prs)),
        "n_comments": n_comments, "comments_source": comments_source,
        "n_repos": ci["n_repos"], "n_errors": prmet["n_errors"],
        "error_rate": (prmet["n_errors"] / len(prs)) if prs else None,
        "error_comments": err_rows,
        "passed_comments": passed,
        "pass_rate": ci["rate"], "ci95": {"lo": ci["lo"], "hi": ci["hi"]},
        "pass_rate_excl_errors": _rate(clean_rows),
        "pr_all_pass": prmet,
        "by_pr_type": dict(sorted(by_type.items())),
        "cost": cost,
        "retrieval": retrieval,
        "evolution": evo,
        "selection_transfer": transfer,
        "provenance": {
            "file": f"results/{path.name}", "salt": d.get("salt"), "code_sha": d.get("code_sha"),
            "library_sha256": d.get("library_sha256"), "library_size": len(lib),
            "harness_version": d.get("harness_version"), "model": d.get("model"),
            "seed": d.get("seed"), "n_requested": d.get("n_requested"), "n_done": d.get("n_done"),
        },
        "warnings": [],
    }
    if d.get("partition") != "test":
        metrics["warnings"].append(f"partition field is {d.get('partition')!r}, expected 'test'")
    if d.get("n_done") is not None and d.get("n_requested") is not None and d.get("n_done") != d.get("n_requested"):
        metrics["warnings"].append(f"INCOMPLETE scored file: n_done={d.get('n_done')} != n_requested={d.get('n_requested')}")
    frozen_path = Path(results_dir or R) / "FROZEN.json"
    if frozen_path.exists():
        try:
            frozen = json.loads(frozen_path.read_text())
            want_lib = (frozen.get("libraries") or {}).get(cond)
            if want_lib and d.get("library_sha256") and want_lib != d.get("library_sha256"):
                metrics["warnings"].append("PROVENANCE: library_sha256 differs from FROZEN.json")
            if frozen.get("code_sha") and d.get("code_sha") and frozen["code_sha"] != d.get("code_sha"):
                metrics["warnings"].append("PROVENANCE: code_sha differs from FROZEN.json")
        except Exception:
            metrics["warnings"].append("could not read results/FROZEN.json for provenance checks")
    metrics["scored_summary"] = {"pass_rate_errors_as_failed": summ.get("pass_rate_errors_as_failed"),
                                 "n_comments": summ.get("n_comments"), "passed": summ.get("passed")}
    if comments_source != "comments":
        metrics["warnings"].append("scored file has no `comments` field; errored PRs' comments are missing "
                                   "from the denominator (complete-case numbers only)")
    if summ.get("pass_rate_errors_as_failed") is not None and ci["rate"] is not None \
            and abs(summ["pass_rate_errors_as_failed"] - ci["rate"]) > 1e-9:
        metrics["warnings"].append(
            f"recomputed comment pass rate {ci['rate']:.4f} differs from the scored summary "
            f"{summ['pass_rate_errors_as_failed']:.4f}; the score file's summary is authoritative")
    if ci["n_repos"] < 2:
        metrics["warnings"].append("fewer than 2 repositories: the clustered bootstrap is degenerate")
    if not cost["duration_recorded"]:
        metrics["warnings"].append("duration_s is 0 in the scored output; wall-clock metrics are unavailable")
    return metrics, rows, prs


# ---------------------------------------------------------------- contrasts
def _pr_map(prs):
    return {p["instance_id"]: p.get("passed") for p in prs if isinstance(p, dict) and p.get("instance_id")}


def _build_contrasts(metrics, prs_by_cond, rows_by_cond, n_perm, n_boot, seed):
    present = [c for c in PREFERRED_ORDER if c in metrics] + \
              [c for c in metrics if c not in PREFERRED_ORDER]
    pairs = []
    if all(c in metrics for c in PRIMARY):
        pairs.append((PRIMARY[0], PRIMARY[1], "primary"))
    else:
        pairs.append((None, None, "primary_unavailable"))
    for c in present:
        if c != "static" and "static" in metrics:
            pairs.append((c, "static", "secondary"))
    for i, c in enumerate(present):
        for c2 in present[i + 1:]:
            if {c, c2} == set(PRIMARY) or "static" in (c, c2):
                continue
            pairs.append((c, c2, "secondary"))

    out, seen = [], set()
    for a, b, role in pairs:
        if a is None:
            out.append({"role": role, "a": None, "b": None,
                        "note": f"needs both conditions {PRIMARY[0]} and {PRIMARY[1]}"})
            continue
        key = (a, b)
        if key in seen:
            continue
        seen.add(key)
        diff = _paired_diff_ci(rows_by_cond[a], rows_by_cond[b], n=n_boot, seed=seed)
        perm_p = analyze.paired_permutation(rows_by_cond[a], rows_by_cond[b], n=n_perm, seed=seed)
        mc = _mcnemar(rows_by_cond[a], rows_by_cond[b])
        pa, pb = _pr_map(prs_by_cond[a]), _pr_map(prs_by_cond[b])
        common = set(pa) & set(pb)
        gained = sum(1 for i in common if pa[i] is True and pb[i] is not True)
        lost = sum(1 for i in common if pb[i] is True and pa[i] is not True)
        rec = {"role": role, "a": a, "b": b,
               "rate_a": metrics[a]["pass_rate"], "rate_b": metrics[b]["pass_rate"],
               "diff": diff["diff"], "diff_pp": (100.0 * diff["diff"]) if diff["diff"] is not None else None,
               "diff_ci95": {"lo": diff["lo"], "hi": diff["hi"]},
               "diff_ci95_pp": {"lo": (100.0 * diff["lo"]) if diff["lo"] is not None else None,
                                "hi": (100.0 * diff["hi"]) if diff["hi"] is not None else None},
               "n_pairs": diff["n_pairs"], "n_repos": diff["n_repos"],
               "permutation_p": perm_p, "mcnemar": mc,
               "prs_compared": len(common), "prs_gained": gained, "prs_lost": lost}
        if mc["discordant"] and mc["discordant"] < EXACT_MAX_DISCORDANT:
            rec["note"] = "exact McNemar; primary permutation p is the pre-registered test"
        out.append(rec)
    return out


# ---------------------------------------------------------------- output
def _print_table(metrics, contrasts):
    print(f"{'condition':<10} {'prs':>4} {'comments':>8} {'pass':>5} {'rate':>7} {'95% CI':>17} "
          f"{'errors':>6} {'PR-all':>7}")
    for c, m in metrics.items():
        ci = m["ci95"]
        ci_s = (f"[{ci['lo']:.3f},{ci['hi']:.3f}]"
                if ci["lo"] is not None else "n/a")
        rate = f"{m['pass_rate']:.3f}" if m["pass_rate"] is not None else "n/a"
        prall = (f"{m['pr_all_pass']['rate_errors_as_failed']:.3f}"
                 if m["pr_all_pass"]["rate_errors_as_failed"] is not None else "n/a")
        print(f"{c:<10} {m['n_prs']:>4} {m['n_comments']:>8} {m['passed_comments']:>5} {rate:>7} "
              f"{ci_s:>17} {m['n_errors']:>6} {prall:>7}")
    for x in contrasts:
        if x.get("a") is None:
            print(f"\ncontrast: {x['note']}")
            continue
        d = x["diff_pp"]
        p = x["permutation_p"]
        mc = x["mcnemar"]
        lo, hi = x["diff_ci95_pp"]["lo"], x["diff_ci95_pp"]["hi"]
        d_s = f"{d:+.1f}pp" if d is not None else "n/a"
        ci_s = f"[{lo:+.1f},{hi:+.1f}]pp" if lo is not None and hi is not None else "[n/a]"
        p_s = f"{p:.4f}" if p is not None else "n/a"
        print(f"\n{x['role']:<9} {x['a']} - {x['b']}: {d_s} (CI {ci_s}), "
              f"perm p={p_s}, McNemar {mc['a_only']}/{mc['b_only']} p={mc['p']:.4g} ({mc['method']}), "
              f"PRs +{x['prs_gained']}/-{x['prs_lost']}")
    for c, m in metrics.items():
        if m["selection_transfer"]["gap_pp"] is not None:
            print(f"transfer {c}: selection={m['selection_transfer']['selection_rate']:.3f} "
                  f"test={m['selection_transfer']['test_rate']:.3f} "
                  f"gap={m['selection_transfer']['gap_pp']:+.1f}pp")


def _csv_rows(metrics, contrasts):
    rows = []
    for c, m in metrics.items():
        cost, ret, tr, evo = m["cost"], m["retrieval"], m["selection_transfer"], m["evolution"]
        rows.append({
            "row_type": "condition", "condition": c, "partition": m["partition"],
            "n_prs": m["n_prs"], "n_comments": m["n_comments"], "n_repos": m["n_repos"],
            "n_errors": m["n_errors"], "error_rate": m["error_rate"],
            "passed_comments": m["passed_comments"], "pass_rate": m["pass_rate"],
            "ci_lo": m["ci95"]["lo"], "ci_hi": m["ci95"]["hi"],
            "pr_all_pass_n": m["pr_all_pass"]["n_all_pass"],
            "pr_all_pass_rate": m["pr_all_pass"]["rate_errors_as_failed"],
            "pr_all_pass_rate_excl_errors": m["pr_all_pass"]["rate_excl_errors"],
            "pass_rate_excl_errors": m["pass_rate_excl_errors"],
            "tokens": cost["tokens"], "tokens_per_pr": cost["tokens_per_pr"],
            "tokens_per_comment": cost["tokens_per_comment"],
            "tokens_per_passing_review": cost["tokens_per_passing_review"],
            "tokens_per_passing_comment": cost["tokens_per_passing_comment"],
            "duration_s": cost["duration_s"], "seconds_per_passing_review": cost["seconds_per_passing_review"],
            "mean_skills_per_review": ret["mean_skills_per_review"],
            "reviews_with_skills": ret["reviews_with_skills"],
            "cap6_reviews": ret["cap6_reviews"],
            "scored_pass_rate": (m.get("scored_summary") or {}).get("pass_rate_errors_as_failed"),
            "selection_rate": tr["selection_rate"], "transfer_gap": tr["gap"],
            "transfer_gap_pp": tr["gap_pp"],
            "accepted_edits": evo.get("accepted_edits"),
            "regressions_per_accepted_edit": evo.get("regressions_per_accepted_edit"),
            "net_gain_per_accepted_edit": evo.get("net_gain_per_accepted_edit"),
            "library_size": m["provenance"]["library_size"],
            "library_sha256": m["provenance"]["library_sha256"],
            "salt": m["provenance"]["salt"], "code_sha": m["provenance"]["code_sha"],
        })
    for x in contrasts:
        if x.get("a") is None:
            continue
        mc = x["mcnemar"]
        rows.append({
            "row_type": "contrast", "condition": f"{x['a']} - {x['b']}", "partition": "test",
            "pass_rate": x["rate_a"], "contrast_vs": x["b"],
            "diff_rate": x["diff"], "diff_pp": x["diff_pp"],
            "diff_ci_lo": x["diff_ci95"]["lo"], "diff_ci_hi": x["diff_ci95"]["hi"],
            "mcnemar_a_only": mc["a_only"], "mcnemar_b_only": mc["b_only"],
            "mcnemar_discordant": mc["discordant"], "mcnemar_method": mc["method"],
            "mcnemar_p": mc["p"], "permutation_p": x["permutation_p"],
            "prs_gained": x["prs_gained"], "prs_lost": x["prs_lost"],
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="compute everything but write no files")
    ap.add_argument("--out-dir", default=str(AN), help="output directory (default results/analysis)")
    ap.add_argument("--boot", type=int, default=10000, help="bootstrap resamples (default 10000)")
    ap.add_argument("--perm", type=int, default=10000, help="permutation draws (default 10000)")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed (default 0)")
    ap.add_argument("--results-dir", default=str(R), help="directory holding test_*.json (default results)")
    a = ap.parse_args(argv)

    results_dir = Path(a.results_dir)
    out_dir = Path(a.out_dir)
    found = _discover(results_dir)
    if not found:
        print("No scored test results found (results/test_<condition>.json).")
        print("The locked test pass has not been run yet: after `results/FROZEN.json` is committed, run")
        print("  python evolve.py score --partition test --condition <c> --salt <name> --unlock-test")
        print("then re-run analyze_final.py. Nothing was written.")
        return 0

    evo_summary, evo_src = None, None
    evo_path = out_dir / "evolution_summary.json"
    if evo_path.exists():
        try:
            evo_summary, evo_src = json.loads(evo_path.read_text()), str(evo_path)
        except Exception as ex:  # malformed aggregate must not block the test analysis
            print(f"warning: cannot read {evo_path} ({ex}); falling back to results/evolution/*.jsonl")

    warnings = [] if evo_summary is not None else ["no results/analysis/evolution_summary.json; evolution "
                                                   "metrics fall back to results/evolution/<condition>.jsonl"]
    metrics, prs_by_cond, rows_by_cond = {}, {}, {}
    for cond, path in found:
        try:
            m, rows, prs = _condition_metrics(cond, path, a.boot, a.seed, evo_summary, results_dir)
        except Exception as ex:
            warnings.append(f"{cond}: could not analyze {path.name} ({type(ex).__name__}: {ex})")
            print(f"warning: {warnings[-1]}")
            continue
        metrics[cond] = m
        prs_by_cond[cond] = prs
        rows_by_cond[cond] = rows
        warnings += [f"{cond}: {w}" for w in m["warnings"]]

    if not metrics:
        print("Scored test files exist but none could be analyzed; nothing was written.")
        return 0

    id_sets = {frozenset(r["instance_id"] for r in rows_by_cond[c]) for c in metrics}
    same_ids = len(id_sets) == 1
    if not same_ids:
        warnings.append("conditions were scored on different PR sets; paired tests use the intersection")

    contrasts = _build_contrasts(metrics, prs_by_cond, rows_by_cond, a.perm, a.boot, a.seed)
    primary = next((x for x in contrasts if x.get("role") == "primary"), None)

    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": "analyze_final.py",
        "partition": "test",
        "conditions": list(metrics),
        "n_test_prs": {c: metrics[c]["n_prs"] for c in metrics},
        "n_test_comments": {c: metrics[c]["n_comments"] for c in metrics},
        "same_pr_set": same_ids,
        "config": {"bootstrap_resamples": a.boot, "permutations": a.perm, "seed": a.seed,
                   "cluster": "repository", "alpha": 0.05, "exact_max_discordant": EXACT_MAX_DISCORDANT,
                   "primary_metric": "comment-level pass rate, errors as failed",
                   "primary_contrast": f"{PRIMARY[0]} - {PRIMARY[1]} (two-sided)"},
        "primary_contrast": primary,
        "contrasts": contrasts,
        "condition_metrics": metrics,
        "evolution_summary_source": evo_src,
        "notes": [
            "The scored outputs are the only test-partition artifacts read: no diffs, comment text or gold "
            "labels are loaded. Outputs go only to --out-dir (default results/analysis).",
            "Token counts are the combined review+repair usage recorded per PR in the scored file; the "
            "per-stage split lives in runs/ledger.jsonl.",
            "Wall-clock duration comes from the scored file; duration_recorded=false means it was 0 and "
            "wall-clock metrics are reported as null.",
            "Retrieval is summarized from the test PRs' skill_ids (ids only); token-budget saturation is not "
            "recorded in the scored output, so only the cap-6 hit count is available.",
            "Selection pass rates are a transfer diagnostic, never held-out performance.",
        ],
        "warnings": warnings,
    }

    _print_table(metrics, contrasts)

    if a.dry_run:
        print("\n--dry-run: no files written.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "final_summary.json"
    json_path.write_text(json.dumps(summary, indent=2))

    csv_path = out_dir / "final_table.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(_csv_rows(metrics, contrasts))

    print(f"\nwrote {json_path}")
    print(f"wrote {csv_path}")
    if warnings:
        print("warnings:")
        for x in warnings:
            print(f"  - {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
