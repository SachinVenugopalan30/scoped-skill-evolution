"""Skill library, retrieval, sanitized feedback, optimizer, selection gate, round loop, CLI."""
import argparse, json, random, re
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
    """static: none. global: global-scope skills only. scoped: global + this PR type's. Library order, stop at budget."""
    if condition == "static":
        return []
    ok = ("global",) if condition == "global" else ("global", pr_type)
    out, used = [], ""
    for s in library:
        if s["scope"] not in ok:
            continue
        if litellm.token_counter(model=reviewer.MODEL, text=used + "\n" + s["text"]) > budget_tokens:
            break
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
    p = ROOT / f"results/skills/{condition}/round_{n}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(library, indent=1))


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
        except harness.BudgetExceeded:
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
    return {"instance_id": inst["instance_id"], "repo": inst["repo"], "pr_type": data.pr_type(inst), "skill_ids": [],
            "findings": [], "per_comment": [], "passed": None, "error": harness.scrub(err), "cost_usd": 0.0}


def evaluate(insts, library, condition, out=None):
    """Results are appended to `out` as they finish, so a BudgetExceeded still leaves partials."""
    out = [] if out is None else out
    stop = None
    with ThreadPoolExecutor(harness.WORKERS) as ex:
        for f in [ex.submit(_safe, i, library, condition) for i in insts]:
            try:
                out.append(f.result())
            except harness.BudgetExceeded as e:
                stop = stop or e
    if stop:
        raise stop
    return out


def run_round(condition, library, dev_prs, sel_prs, gated=True):
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
    rec = {"condition": condition, "edits": edits, "dev_passed": {r["instance_id"]: r["passed"] for r in dev}}
    if not edits:
        rec.update(accept=False, gained=0, regressed=0, excluded=[], note="no edits")
    elif not gated:
        rec.update(accept=True, gained=None, regressed=None, excluded=[], note="ungated: selection not evaluated")
    else:
        # ponytail: both edits are one candidate; per-edit gating needs a selection pass per edit (2x cost)
        base = evaluate(sel_prs, library, condition)
        new = evaluate(sel_prs, cand, condition)
        rec.update(gate({r["instance_id"]: r["passed"] for r in base}, {r["instance_id"]: r["passed"] for r in new}))
    rec["cost_usd"] = round(harness.spent() - s0, 4)
    log = ROOT / f"results/evolution/{condition}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return (cand if rec["accept"] else library), rec


# ---------- sampling ----------
def sample(partition, n, seed):
    if partition == "test":
        raise ValueError("locked test partition: our system may not run on it")
    ids = json.load(open(ROOT / "results/splits.json"))["instance_ids"][partition]
    inst = harness.instances()
    by = {}
    for i in sorted(ids):
        if i not in harness.NO_IMAGE:
            by.setdefault(data.pr_type(inst[i]), []).append(i)
    total = sum(map(len, by.values()))
    n = min(n, total)
    quota = {t: n * len(v) // total for t, v in by.items()}
    for t in sorted(by, key=lambda t: (-(n * len(by[t]) % total), t))[: n - sum(quota.values())]:
        quota[t] += 1  # largest remainder
    rng = random.Random(seed)
    return sorted(i for t in sorted(by) for i in rng.sample(by[t], quota[t]))


# ---------- CLI ----------
def comment_rows(results):
    return [{"instance_id": r["instance_id"], "repo": r["repo"], "comment_index": p["comment_index"], "passed": p["passed"]}
            for r in results for p in r["per_comment"] if p["passed"] is not None]


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
            except harness.BudgetExceeded as ex:
                print("STOPPED:", ex)
            out = {"partition": a.partition, "seed": a.seed, "n_requested": len(ids), "n_done": len(res), "prs": res,
                   "comments": comment_rows(res), "cost_usd": round(sum(r["cost_usd"] for r in res), 4)}
            (ROOT / f"results/static_{a.partition}.json").write_text(json.dumps(out, indent=1))
            print(f"static {a.partition}: {len(res)}/{len(ids)} PRs, {len(out['comments'])} comments, passed "
                  f"{sum(c['passed'] for c in out['comments'])}, cost ${out['cost_usd']}")
        else:
            dev = [inst[i] for i in sample("development", a.dev_n, a.seed)]
            sel = [inst[i] for i in sample("selection", a.sel_n, a.seed)]
            lib, ran = [], 0
            save_snapshot(a.condition, 0, lib)
            try:
                for r in range(1, a.rounds + 1):
                    lib, rec = run_round(a.condition, lib, dev, sel, not a.no_gate)
                    save_snapshot(a.condition, r, lib)
                    ran = r
                    print(f"round {r}: accept={rec['accept']} gained={rec['gained']} regressed={rec['regressed']} cost=${rec['cost_usd']}")
            except harness.BudgetExceeded as ex:
                print("STOPPED:", ex)
                p = ROOT / f"results/evolution/{a.condition}.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a") as f:
                    f.write(json.dumps({"condition": a.condition, "aborted": "budget_exceeded", "round": ran + 1}) + "\n")
            print(f"{ran}/{a.rounds} rounds completed; library has {len(lib)} skills; total spent ${harness.spent():.2f}")
    except harness.BudgetExceeded as ex:
        print("STOPPED:", ex)


if __name__ == "__main__":
    main()
