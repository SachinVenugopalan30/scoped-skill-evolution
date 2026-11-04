"""Skill library, retrieval, sanitized feedback, optimizer, selection gate, round loop, CLI."""
import argparse, json, random, re, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import litellm

import data, harness, reviewer

ROOT = data.ROOT
SCOPES = ("global", "bug_fix", "feature", "other")
MAX_EDITS, MAX_WORDS = 2, 60
OPT_RESERVE_USD = 0.3
PATH_RE = re.compile(r"(?:[\w.~\-]*/)+[\w.\-]*|\b[\w\-]+\.(?:py|js|ts|tsx|jsx|go|md|rst|txt|json|ya?ml|toml|cfg|ini|rs|java|c|h|cpp|html|css|sh)\b")
LINE_RE = re.compile(r"\blines?\s*\d+(?:\s*[-–]\s*\d+)?", re.I)


# ---------- library + retrieval ----------
def retrieve(library, pr_type, condition, budget_tokens=400):
    """static: none. global: global-scope skills only. scoped: this PR type's skills first, then global ones.
    Ordering rule: within each group, library order. A skill that would exceed the budget is skipped and later,
    smaller ones may still fit. Same budget for both conditions."""
    if condition == "static":
        return []
    groups = [["global"]] if condition == "global" else [[pr_type], ["global"]]
    out, used = [], ""
    for scopes in groups:
        for s in library:
            if s["scope"] in scopes and litellm.token_counter(model=reviewer.MODEL, text=used + "\n" + s["text"]) <= budget_tokens:
                used += "\n" + s["text"]
                out.append(s)
    return out


def apply_edits(library, edits):
    lib, n = [dict(s) for s in library], max([int(s["id"][1:]) for s in library if s["id"][1:].isdigit()], default=0)
    for e in edits:
        if e["op"] == "add":
            n += 1
            lib.append({"id": f"s{n}", "scope": e["scope"], "text": e["text"]})
        elif e["op"] == "replace":
            for s in lib:
                if s["id"] == e["id"]:
                    s["text"] = e["text"]
        else:
            lib = [s for s in lib if s["id"] != e["id"]]
    return lib


def save_snapshot(condition, n, library):
    harness.write_json(ROOT / f"results/skills/{condition}/round_{n}.json", library)


# ---------- sanitizer ----------
def _clean(t, forbid):
    for f in forbid:
        t = re.sub(re.escape(f), "[redacted]", t, flags=re.I)
    return LINE_RE.sub("", PATH_RE.sub("[path]", t))[:600]


def sanitize_feedback(pr_type, findings, per_comment, forbid=()):
    """Allowlist: built from scratch. Pass/fail is per concern, so feedback is PR-level (k of n)."""
    return {"pr_type": pr_type,
            "findings": [{"issue_header": _clean(f["issue_header"], forbid), "issue_content": _clean(f["issue_content"], forbid)}
                         for f in findings],
            "passed": sum(1 for p in per_comment if p["passed"] is True), "total": len(per_comment)}


def assert_clean(payload, forbidden):
    s = (payload if isinstance(payload, str) else json.dumps(payload)).lower()
    for f in forbidden:
        if f and (f.lower() in s or json.dumps(f)[1:-1].lower() in s):
            raise ValueError(f"forbidden string in optimizer payload: {f[:40]!r}")


def forbidden_for(inst, findings):
    """Strings that must never reach the optimizer for this PR."""
    iid, repo = inst["instance_id"], inst["repo"]
    files = [f["file"] for f in findings]
    redact = [x for x in [iid, repo, repo.split("/")[-1]] + files + [f.split("/")[-1] for f in files] if len(x) >= 4]
    # short names (repo tail, basenames) are redacted but not asserted: a common word would trip the check on the system prompt
    gold = [c["text"] for c in inst["reference_review_comments"]] + [e["test_code"] for _, _, e in harness.stage4_tests(inst)]
    return redact, [iid, repo] + files + gold


# ---------- optimizer ----------
def parse_edits(raw, library, condition):
    ids, out = {s["id"] for s in library}, []
    for e in reviewer.extract_list(raw, "edits") or []:
        if not isinstance(e, dict) or e.get("op") not in ("add", "replace", "delete"):
            continue
        text = " ".join(str(e.get("text") or "").split()[:MAX_WORDS])
        if e["op"] == "delete":
            if e.get("id") in ids:
                out.append({"op": "delete", "id": e["id"]})
        elif not text or (e["op"] == "replace" and e.get("id") not in ids):
            continue
        elif e["op"] == "replace":
            out.append({"op": "replace", "id": e["id"], "text": text})
        else:
            scope = "global" if condition == "global" else e.get("scope")
            if scope in SCOPES:
                out.append({"op": "add", "scope": scope, "text": text})
    return out[:MAX_EDITS]


def propose_edits(library, feedback, condition, forbidden=(), model=reviewer.MODEL):
    scopes = "scope must be \"global\"" if condition == "global" else f"scope is one of {list(SCOPES)}; use a PR-type scope only for guidance specific to that type"
    system = ("You improve a code-review agent's skill library. Skills are short reusable review guidance injected into its prompt. "
              "Feedback shows, per reviewed PR, the review findings and how many of the PR's independent concerns were then resolved "
              "(passed of total). Write general guidance, never facts about a specific PR, file or project. "
              f"Reply with ONLY a JSON list of at most {MAX_EDITS} edits, each {{\"op\": \"add\"|\"replace\"|\"delete\", \"scope\", \"id\" (replace/delete), "
              f"\"text\" (add/replace, at most {MAX_WORDS} words)}}. For add, {scopes}.")
    user = json.dumps({"library": library, "feedback": feedback}, indent=1)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    assert_clean(msgs, forbidden)  # exact payload, right before the call
    harness.check_budget(OPT_RESERVE_USD)
    resp = litellm.completion(model=model, messages=msgs, temperature=0, max_tokens=1000, num_retries=2)
    harness.record("optimizer", "round", reviewer.cost_of(resp))
    return parse_edits(resp.choices[0].message.content or "", library, condition)


# ---------- gate ----------
def gate(baseline, candidate):
    """baseline/candidate: pr_id -> True/False/None(error). Errored PRs are excluded, never regressions."""
    excluded = sorted(p for p in set(baseline) | set(candidate) if baseline.get(p) is None or candidate.get(p) is None)
    both = [p for p in baseline if p in candidate and p not in excluded]
    gained = sum(1 for p in both if not baseline[p] and candidate[p])
    regressed = sum(1 for p in both if baseline[p] and not candidate[p])
    return {"accept": gained >= 1 and regressed <= 1, "gained": gained, "regressed": regressed, "excluded": excluded}


# ---------- evaluation ----------
def evaluate_pr(inst, library, condition):
    skills = retrieve(library, data.pr_type(inst), condition)
    rv = harness.run_review(inst, [s["text"] for s in skills])
    out = {"instance_id": inst["instance_id"], "repo": inst["repo"], "pr_type": data.pr_type(inst), "skill_ids": [s["id"] for s in skills],
           "findings": rv["findings"], "per_comment": [], "passed": None, "error": rv.get("error"), "cost_usd": rv["cost_usd"]}
    if out["error"]:
        return out
    rep = harness.repair_pr(inst, rv["findings"])
    return {**out, "per_comment": rep["per_comment"], "passed": harness.pr_passed(rep), "error": rep["error"],
            "cost_usd": rv["cost_usd"] + rep["cost_usd"]}


def _safe(inst, library, condition):
    for attempt in range(2):  # retried once, then recorded
        try:
            return evaluate_pr(inst, library, condition)
        except harness.StopRun:
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
    return {"instance_id": inst["instance_id"], "repo": inst["repo"], "pr_type": data.pr_type(inst), "skill_ids": [],
            "findings": [], "per_comment": [], "passed": None, "error": harness.scrub(err), "cost_usd": 0.0}


def evaluate(insts, library, condition, out=None):
    """Results are appended to `out` as they finish. On StopRun (budget/fatal) or Ctrl-C, queued work is cancelled so it
    stops paying, in-flight results are still collected, then the stop is re-raised."""
    out = [] if out is None else out
    stop, seen = None, set()
    ex = ThreadPoolExecutor(harness.WORKERS)
    futs = [ex.submit(_safe, i, library, condition) for i in insts]
    try:
        for f in futs:
            if f.cancelled():
                continue
            try:
                out.append(f.result()); seen.add(id(f))
            except harness.StopRun as e:
                seen.add(id(f)); stop = stop or e
                ex.shutdown(wait=False, cancel_futures=True)
    except KeyboardInterrupt:
        stop = KeyboardInterrupt()
        ex.shutdown(wait=False, cancel_futures=True)
    finally:
        ex.shutdown(wait=True)
    for f in futs:  # in-flight work that finished while we were stopping
        if id(f) not in seen and f.done() and not f.cancelled() and f.exception() is None:
            out.append(f.result())
    if stop:
        raise stop
    return out


def summarize(results):
    """Main number is pass_rate_errors_as_failed (an errored PR's comments count as failed, like released tools with
    missing results); pass_rate_excl_errors drops errored PRs."""
    inst = harness.instances()
    err = [r["instance_id"] for r in results if r["passed"] is None]
    total = sum(len(inst[r["instance_id"]]["reference_review_comments"]) for r in results)
    ok = [p for r in results if r["passed"] is not None for p in r["per_comment"]]
    k = sum(p["passed"] is True for p in ok)
    return {"n_prs": len(results), "n_errors": len(err), "errored_prs": err, "n_comments": total, "passed": k,
            "pass_rate_errors_as_failed": k / total if total else None, "pass_rate_excl_errors": k / len(ok) if ok else None}


def run_round(condition, library, dev_prs, sel_prs, gated=True, run_id=None):
    """dev_prs/sel_prs: instance dicts. Returns (new_library, record)."""
    s0 = harness.spent()
    dev = evaluate(dev_prs, library, condition)
    fb, forbidden = [], []
    for r, inst in zip(dev, dev_prs):
        if r["passed"] is None:
            continue
        redact, check = forbidden_for(inst, r["findings"])
        fb.append(sanitize_feedback(r["pr_type"], r["findings"], r["per_comment"], redact))
        forbidden += check
    edits = propose_edits(library, fb, condition, forbidden) if fb else []
    cand = apply_edits(library, edits)
    rec = {"condition": condition, "edits": edits, "dev_passed": {r["instance_id"]: r["passed"] for r in dev},
           "dev": summarize(dev)}
    if not edits:
        rec.update(accept=False, gained=0, regressed=0, excluded=[], note="no edits")
    elif not gated:
        rec.update(accept=True, gained=None, regressed=None, excluded=[], note="ungated: selection not evaluated")
    else:
        # ponytail: both edits are one candidate; per-edit gating needs a selection pass per edit (2x cost)
        base = evaluate(sel_prs, library, condition)
        new = evaluate(sel_prs, cand, condition)
        rec.update(gate({r["instance_id"]: r["passed"] for r in base}, {r["instance_id"]: r["passed"] for r in new}))
        rec.update(sel_base=summarize(base), sel_new=summarize(new))
    rec["run_id"] = run_id
    rec["cost_usd"] = round(harness.spent() - s0, 4)
    harness.append_json(ROOT / f"results/evolution/{condition}.jsonl", rec)
    harness.cleanup_images()  # round is over: selection baseline and candidate have both used the images
    return (cand if rec["accept"] else library), rec


# ---------- sampling ----------
def eligible(partition):
    """({pr_type: [ids]}, excluded). Same filter for every condition: no docker image, or diff too long for the reviewer cap."""
    if partition == "test":
        raise ValueError("locked test partition: our system may not run on it")
    ids = json.load(open(ROOT / "results/splits.json"))["instance_ids"][partition]
    inst = harness.instances()
    by, ex = {}, {"no_image": [], "oversize": []}
    for i in sorted(ids):
        if i in harness.NO_IMAGE:
            ex["no_image"].append(i)
        elif len(inst[i]["commit_to_review"]["patch_to_review"]) > reviewer.MAX_DIFF_CHARS:
            ex["oversize"].append(i)
        else:
            by.setdefault(data.pr_type(inst[i]), []).append(i)
    return by, ex


def sample(partition, n, seed):
    """Seeded and nested: sample(p, m, seed) == sample(p, n, seed)[:m] for m < n. Each type's PRs are shuffled with the seed,
    then types are interleaved by largest deficit against their population share, so every prefix is proportionally
    stratified to within one PR. Returned in that order (not sorted)."""
    by, _ = eligible(partition)
    rng = random.Random(seed)
    pools = {t: rng.sample(by[t], len(by[t])) for t in sorted(by)}
    total, taken, out = sum(map(len, pools.values())), {t: 0 for t in pools}, []
    for k in range(min(n, total)):
        t = max((t for t in pools if taken[t] < len(pools[t])), key=lambda t: (len(pools[t]) * (k + 1) / total - taken[t], t))
        out.append(pools[t][taken[t]]); taken[t] += 1
    return out


# ---------- CLI ----------
def comment_rows(results):
    """One row per reference comment; comments of errored PRs are rows with passed False, error True (errors-as-failed)."""
    inst = harness.instances()
    rows = []
    for r in results:
        if r["passed"] is None:
            rows += [{"instance_id": r["instance_id"], "repo": r["repo"], "comment_index": i, "passed": False, "error": True}
                     for i in range(len(inst[r["instance_id"]]["reference_review_comments"]))]
        else:
            rows += [{"instance_id": r["instance_id"], "repo": r["repo"], "comment_index": p["comment_index"],
                      "passed": p["passed"], "error": False} for p in r["per_comment"]]
    return rows


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("static")
    s.add_argument("--partition", required=True)
    s.add_argument("--n", type=int, required=True)
    e = sub.add_parser("evolve")
    e.add_argument("--condition", choices=["global", "scoped"], required=True)
    e.add_argument("--dev-n", type=int, required=True)
    e.add_argument("--sel-n", type=int, required=True)
    e.add_argument("--rounds", type=int, required=True)
    e.add_argument("--no-gate", action="store_true")
    for p in (s, e):
        p.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    harness.setup()
    inst = harness.instances()
    try:
        if a.cmd == "static":
            ids, res = sample(a.partition, a.n, a.seed), []
            try:
                evaluate([inst[i] for i in ids], [], "static", res)
            except (harness.StopRun, KeyboardInterrupt) as ex:
                print("STOPPED:", repr(ex) if isinstance(ex, KeyboardInterrupt) else ex)
            out = {"condition": "static", "partition": a.partition, "seed": a.seed, "n_requested": len(ids), "n_done": len(res),
                   "excluded": {k: {"n": len(v), "ids": v} for k, v in eligible(a.partition)[1].items()},
                   "summary": summarize(res), "prs": res, "comments": comment_rows(res),
                   "cost_usd": round(sum(r["cost_usd"] for r in res), 4)}
            harness.write_json(ROOT / f"results/static_{a.partition}.json", out)
            print(f"static {a.partition}: {json.dumps(out['summary'])} cost ${out['cost_usd']}")
        else:
            run_id = uuid.uuid4().hex[:8]  # separates resumed runs' headers/aborted records in the append-only log
            dev_ids, sel_ids = sample("development", a.dev_n, a.seed), sample("selection", a.sel_n, a.seed)
            dev, sel = [inst[i] for i in dev_ids], [inst[i] for i in sel_ids]
            harness.append_json(ROOT / f"results/evolution/{a.condition}.jsonl", {
                "run_id": run_id, "condition": a.condition, "seed": a.seed, "gated": not a.no_gate, "dev_ids": dev_ids, "sel_ids": sel_ids,
                "excluded": {p: {k: len(v) for k, v in eligible(p)[1].items()} for p in ("development", "selection")}})
            lib, ran = [], 0
            save_snapshot(a.condition, 0, lib)
            try:
                for r in range(1, a.rounds + 1):
                    lib, rec = run_round(a.condition, lib, dev, sel, not a.no_gate, run_id)
                    save_snapshot(a.condition, r, lib)
                    ran = r
                    print(f"round {r}: accept={rec['accept']} gained={rec['gained']} regressed={rec['regressed']} "
                          f"dev errors-as-failed={rec['dev']['pass_rate_errors_as_failed']} n_errors={rec['dev']['n_errors']} cost=${rec['cost_usd']}")
            except (harness.StopRun, KeyboardInterrupt) as ex:
                print("STOPPED:", repr(ex) if isinstance(ex, KeyboardInterrupt) else ex)
                harness.append_json(ROOT / f"results/evolution/{a.condition}.jsonl",
                                    {"run_id": run_id, "condition": a.condition, "aborted": str(ex) or type(ex).__name__, "round": ran + 1})
            print(f"{ran}/{a.rounds} rounds completed; library has {len(lib)} skills; total spent ${harness.spent():.2f}")
    except harness.StopRun as ex:
        print("STOPPED:", ex)
    finally:
        harness.cleanup_images()  # also after stops and Ctrl-C


if __name__ == "__main__":
    main()
