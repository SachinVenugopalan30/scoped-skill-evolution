"""Skill library, retrieval, sanitized feedback, optimizer, selection gate, round loop, CLI."""
import argparse, hashlib, json, random, re, subprocess, time, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import litellm

import data, harness, reviewer

ROOT = data.ROOT
SCOPES = ("global", "bug_fix", "feature", "other")
MAX_EDITS, MAX_WORDS = 1, 60
LAST_OPT_RAW = None  # raw text of the latest optimizer reply, logged with the round record
MAX_SKILLS = 6            # retrieved skills per PR (hard cap, before the token budget)
RETRIEVAL_BUDGET = 400    # tokens of skill text injected into one review prompt
OPT_RESERVE_USD = 0.3
PATH_RE = re.compile(r"(?:[\w.~\-]*/)+[\w.\-]*|\b[\w\-]+\.(?:py|js|ts|tsx|jsx|go|md|rst|txt|json|ya?ml|toml|cfg|ini|rs|java|c|h|cpp|html|css|sh)\b")
LINE_RE = re.compile(r"\blines?\s*\d+(?:\s*[-–]\s*\d+)?", re.I)
SNAP_RE = re.compile(r"^round_(\d+)\.json$")


# ---------- library + retrieval ----------
def _order(s, i=0):
    """Integer order of a skill; legacy snapshots without one fall back to list position (oldest first)."""
    try:
        return int(s.get("order"))
    except (TypeError, ValueError, AttributeError):
        return i


def normalize_library(library):
    """Give every skill an explicit order, preserving existing ones. Used when loading old snapshots."""
    return [{**s, "order": _order(s, i)} for i, s in enumerate(library)]


def _norm_text(text):
    return " ".join(str(text).lower().split())


def retrieve(library, pr_type, condition, budget_tokens=RETRIEVAL_BUDGET):
    """static: none. global: global-scope skills only. scoped: this PR type's skills first, then global ones.
    Within each group, newest first (order descending). Duplicate texts are injected once, at most MAX_SKILLS skills are
    returned, and the token budget still uses skip-and-continue: a skill that does not fit is skipped and later, smaller
    ones may still fit. Same budget for both conditions."""
    if condition == "static":
        return []
    library = normalize_library(library)
    groups = [["global"]] if condition == "global" else [[pr_type], ["global"]]
    out, seen, used = [], set(), ""
    for scopes in groups:
        ranked = sorted((( _order(s, i), i, s) for i, s in enumerate(library) if s.get("scope") in scopes),
                        key=lambda t: (-t[0], t[1]))
        for _, _, s in ranked:
            if len(out) >= MAX_SKILLS:
                break
            key = _norm_text(s["text"])
            if key in seen:
                continue
            candidate = used + "\n" + s["text"]
            if litellm.token_counter(model=reviewer.MODEL, text=candidate) > budget_tokens:
                continue
            used = candidate
            seen.add(key)
            out.append(s)
        if len(out) >= MAX_SKILLS:
            break
    return out


def _max_order(library):
    return max((_order(s, i) for i, s in enumerate(library)), default=0)


def apply_edits(library, edits):
    """Adds and replaces get order = max+1 (so they are the newest and retrievable first); deletes remove the skill."""
    lib, n = normalize_library(library), max([int(s["id"][1:]) for s in library if s["id"][1:].isdigit()], default=0)
    mx = _max_order(lib)
    for e in edits:
        if e["op"] == "add":
            n += 1
            mx += 1
            lib.append({"id": f"s{n}", "scope": e["scope"], "text": e["text"], "order": mx})
        elif e["op"] == "replace":
            for s in lib:
                if s["id"] == e["id"]:
                    mx += 1
                    s["text"], s["order"] = e["text"], mx
                    break
        else:
            lib = [s for s in lib if s["id"] != e["id"]]
    return lib


def save_snapshot(condition, n, library, overwrite=True):
    """round_<n>.json under results/skills/<condition>/. Resume never overwrites an earlier snapshot."""
    path = ROOT / f"results/skills/{condition}/round_{n}.json"
    if path.exists() and not overwrite:
        return path
    harness.write_json(path, normalize_library(library))
    return path


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


def _feedback_forbid(records):
    """Redaction/assertion lists derived only from the per-PR records (no instance lookup, no gold)."""
    redact, check = set(), set()
    for r in records:
        iid, repo = r.get("instance_id") or "", r.get("repo") or ""
        files = [f["file"] for f in (r.get("findings") or [])
                 if isinstance(f, dict) and isinstance(f.get("file"), str) and f["file"]]
        for x in [iid, repo, repo.split("/")[-1] if repo else ""] + files + [f.split("/")[-1] for f in files]:
            if isinstance(x, str) and len(x) >= 4:
                redact.add(x)
        for x in [iid, repo] + files:
            if isinstance(x, str) and len(x) >= 4:  # too-short strings would match ordinary words
                check.add(x)
    # longest first so a path is removed before its basename
    return sorted(redact, key=len, reverse=True), sorted(check, key=len, reverse=True)


# ---------- feedback ----------
def build_feedback(dev_results, library, condition, rejected=()):
    """Compact, sanitized payload for the optimizer: aggregate pass/fail per type, a few failed-first examples, per-skill
    retrieval usefulness, and the edits that were already rejected (so they are not proposed again)."""
    records = [r for r in dev_results if isinstance(r, dict) and r.get("instance_id")]
    redact, check = _feedback_forbid(records)
    overall = {"prs": 0, "passed_prs": 0, "comments_passed": 0, "comments_total": 0, "errors": 0}
    by_type, examples = {}, []
    skills = {s["id"]: {"retrieved": 0, "passed_prs": 0} for s in library}
    for r in records:
        per_comment = [p for p in (r.get("per_comment") or []) if isinstance(p, dict)]
        cp = sum(1 for p in per_comment if p.get("passed") is True)
        ct = len(per_comment)
        ptype = r.get("pr_type") or "other"
        st = by_type.setdefault(ptype, {"prs": 0, "passed_prs": 0, "comments_passed": 0, "comments_total": 0})
        st["prs"] += 1
        st["comments_passed"] += cp
        st["comments_total"] += ct
        overall["prs"] += 1
        overall["comments_passed"] += cp
        overall["comments_total"] += ct
        if r.get("passed") is True:
            overall["passed_prs"] += 1
            st["passed_prs"] += 1
        if r.get("passed") is None:
            overall["errors"] += 1
        for sid in r.get("skill_ids") or []:
            d = skills.setdefault(sid, {"retrieved": 0, "passed_prs": 0})
            d["retrieved"] += 1
            if r.get("passed") is True:
                d["passed_prs"] += 1
        if r.get("passed") is not None:
            findings = [{"issue_header": _clean(f.get("issue_header") or "", redact),
                         "issue_content": _clean(f.get("issue_content") or "", redact)}
                        for f in (r.get("findings") or [])[:3] if isinstance(f, dict)]
            examples.append({"pr_type": ptype, "findings": findings, "passed": cp, "total": ct,
                             "_failed": r.get("passed") is False})
    examples.sort(key=lambda e: 0 if e["_failed"] else 1)  # failed PRs first, stable otherwise
    examples = [{k: v for k, v in e.items() if k != "_failed"} for e in examples[:12]]

    rejected_edits = []
    for e in rejected or ():
        if not isinstance(e, dict) or e.get("outcome") not in (None, "rejected"):
            continue
        text = _clean(str(e.get("text") or ""), redact) if e.get("text") else ""
        if not text and not e.get("id"):
            continue
        item = {"op": e.get("op") or "add", "outcome": "rejected"}
        if e.get("scope"):
            item["scope"] = e["scope"]
        if e.get("id"):
            item["id"] = e["id"]
        if text:
            item["text"] = text
        rejected_edits.append(item)

    payload = {"overall": overall, "by_type": dict(sorted(by_type.items())), "examples": examples,
               "skills": {k: skills[k] for k in skills}, "rejected": rejected_edits[-12:]}
    assert_clean(payload, check)  # exact final payload, before it is placed in a prompt
    return payload


# ---------- optimizer ----------
def _edit_items(raw):
    """Edits from the optimizer reply in any shape the model has used: a bare list, {"edits": [...]},
    a list wrapped under another key ({"library": [...]}), or a single bare edit object ({"op": ...})."""
    items = reviewer.extract_list(raw, "edits")
    if items is not None:
        return items
    i = raw.find("{")
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[i:]) if i >= 0 else (None, 0)
    except ValueError:
        return []
    return [obj] if isinstance(obj, dict) and "op" in obj else []


def parse_edits(raw, library, condition):
    ids, out = {s["id"] for s in library}, []
    for e in _edit_items(raw):
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


def _looks_like_records(x):
    return isinstance(x, list) and bool(x) and all(isinstance(e, dict) and "instance_id" in e for e in x)


def _looks_like_edits(x):
    return isinstance(x, list) and bool(x) and all(isinstance(e, dict) and ("op" in e or "outcome" in e) for e in x)


def propose_edits(library, feedback, condition, forbidden=(), model=reviewer.MODEL, round_no=None):
    # Accept an already-built payload or raw dev records; the latter is built into a payload here (build_feedback).
    if _looks_like_records(library):  # also tolerate propose_edits(dev_results, library, condition, rejected)
        dev_results, real_library = library, feedback
        extra = forbidden
        feedback = build_feedback(dev_results, real_library, condition, extra if _looks_like_edits(extra) else ())
        # Keep the iid/repo/file assertion even for the legacy call shape; the caller may add gold strings on top.
        forbidden = _feedback_forbid(dev_results)[1]
        library = real_library
    elif _looks_like_records(feedback):
        feedback = build_feedback(feedback, library, condition)
    scopes = "scope must be \"global\"" if condition == "global" else f"scope is one of {list(SCOPES)}; use a PR-type scope only for guidance specific to that type"
    system = ("You improve a code-review agent's skill library. Skills are short reusable review guidance injected into its prompt. "
              "Feedback shows, per reviewed PR, the review findings and how many of the PR's independent concerns were then resolved "
              "(passed of total). Write general guidance, never facts about a specific PR, file or project. "
              "One rule: do not repeat rejected edits - the feedback's `rejected` list holds edits that were already "
              "proposed and failed the selection gate, so propose something materially different. "
              f"Reply with ONLY a JSON list of at most {MAX_EDITS} edits, each {{\"op\": \"add\"|\"replace\"|\"delete\", \"scope\", \"id\" (replace/delete), "
              f"\"text\" (add/replace, at most {MAX_WORDS} words)}}. For add, {scopes}.")
    user = json.dumps({"library": library, "feedback": feedback}, indent=1)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    assert_clean(msgs, forbidden)  # exact payload, right before the call
    harness.check_budget(OPT_RESERVE_USD)
    t0 = time.time()
    resp = litellm.completion(model=model, messages=msgs, temperature=0, max_tokens=1000, num_retries=2, **reviewer.llm_kwargs())
    label = f"{condition}:r{round_no}" if round_no is not None else "round"
    harness.record("optimizer", label, reviewer.cost_of(resp), tokens=reviewer._usage_delta(resp),
                   duration_s=round(time.time() - t0, 1), model=model)
    global LAST_OPT_RAW
    LAST_OPT_RAW = resp.choices[0].message.content or ""
    return parse_edits(LAST_OPT_RAW, library, condition)


# ---------- gate ----------
def gate(baseline, candidate, min_gain=1, max_regress=1):
    """baseline/candidate: pr_id -> True/False/None(error). Errored PRs are excluded, never regressions.
    min_gain is the required NET gain (gained - regressed) per PROTOCOL.md; max_regress still bounds churn."""
    excluded = sorted(p for p in set(baseline) | set(candidate) if baseline.get(p) is None or candidate.get(p) is None)
    both = [p for p in baseline if p in candidate and p not in excluded]
    gained = sum(1 for p in both if not baseline[p] and candidate[p])
    regressed = sum(1 for p in both if baseline[p] and not candidate[p])
    net = gained - regressed
    return {"accept": net >= min_gain and regressed <= max_regress, "gained": gained, "regressed": regressed,
            "net": net, "excluded": excluded}


# ---------- evaluation ----------
def _run_review(inst, skills, salt=None):
    """Pass a deterministic salt only when one is supplied; keeps 3-argument test doubles working."""
    return harness.run_review(inst, skills, salt=salt) if salt else harness.run_review(inst, skills)


def _repair_pr(inst, findings, salt=None):
    return harness.repair_pr(inst, findings, salt=salt) if salt else harness.repair_pr(inst, findings)


def _add_usage(*usages):
    """Sum token usage from either shape: litellm/reviewer (prompt_tokens/completion_tokens) or
    harness.parse_claude (input/output/cache_*). Repair is the dominant consumer, so dropping its shape would
    under-report the whole run."""
    prompt = completion = total = 0
    for u in usages:
        if not isinstance(u, dict):
            continue
        if "prompt_tokens" in u or "completion_tokens" in u:
            p = int(u.get("prompt_tokens") or 0)
            c = int(u.get("completion_tokens") or 0)
        else:
            p = (int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                 + int(u.get("cache_read_input_tokens") or 0))
            c = int(u.get("output_tokens") or 0)
        t = u.get("total_tokens")
        prompt += p
        completion += c
        total += int(t) if t is not None else p + c
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def evaluate_pr(inst, library, condition, salt=None):
    t0 = time.time()
    skills = retrieve(library, data.pr_type(inst), condition)
    rv = _run_review(inst, [s["text"] for s in skills], salt=salt)
    out = {"instance_id": inst["instance_id"], "repo": inst["repo"], "pr_type": data.pr_type(inst), "skill_ids": [s["id"] for s in skills],
           "findings": rv["findings"], "per_comment": [], "passed": None, "error": rv.get("error"),
           "cost_usd": rv.get("cost_usd") or 0.0, "usage": _add_usage(rv.get("usage")), "duration_s": 0.0}
    if out["error"]:
        out["duration_s"] = round(time.time() - t0, 1)
        return out
    rep = _repair_pr(inst, rv["findings"], salt=salt)
    return {**out, "per_comment": rep["per_comment"], "passed": harness.pr_passed(rep), "error": rep.get("error"),
            "cost_usd": out["cost_usd"] + (rep.get("cost_usd") or 0.0),
            "usage": _add_usage(rv.get("usage"), rep.get("usage")),
            "duration_s": round(time.time() - t0, 1)}


def _safe(inst, library, condition, salt=None):
    for attempt in range(2):  # retried once, then recorded
        try:
            return evaluate_pr(inst, library, condition, salt=salt)
        except harness.StopRun:
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
    return {"instance_id": inst["instance_id"], "repo": inst["repo"], "pr_type": data.pr_type(inst), "skill_ids": [],
            "findings": [], "per_comment": [], "passed": None, "error": harness.scrub(err), "cost_usd": 0.0,
            "usage": _add_usage(), "duration_s": 0.0}


def evaluate(insts, library, condition, out=None, salt=None):
    """Results are appended to `out` as they finish, then returned in the input PR order. On StopRun (budget/fatal) or
    Ctrl-C, queued work is cancelled so it stops paying, in-flight results are still collected, then the stop is re-raised."""
    out = [] if out is None else out
    order = {i["instance_id"]: k for k, i in enumerate(insts)}
    stop, seen = None, set()
    ex = ThreadPoolExecutor(harness.WORKERS)
    if salt:
        futs = [ex.submit(_safe, i, library, condition, salt) for i in insts]
    else:
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
    out.sort(key=lambda r: order.get(r.get("instance_id"), len(order)))  # input order, not completion order
    if stop:
        raise stop
    return out


def summarize(results):
    """Main number is pass_rate_errors_as_failed (an errored PR's comments count as failed, like released tools with
    missing results); pass_rate_excl_errors / pass_rate_comments_excl_errors drop errored PRs."""
    inst = harness.instances()
    err = [r["instance_id"] for r in results if r["passed"] is None]
    total = sum(len(inst[r["instance_id"]]["reference_review_comments"]) for r in results)
    ok = [p for r in results if r["passed"] is not None for p in r["per_comment"]]
    k = sum(p["passed"] is True for p in ok)
    usage = _add_usage(*(r.get("usage") for r in results))
    duration = sum(float(r.get("duration_s") or 0) for r in results)
    rate_excl = k / len(ok) if ok else None
    return {"n_prs": len(results), "n_errors": len(err), "errored_prs": err, "n_comments": total, "passed": k,
            "pass_rate_errors_as_failed": k / total if total else None, "pass_rate_excl_errors": rate_excl,
            "pass_rate_comments_excl_errors": rate_excl,
            "tokens": usage["total_tokens"], "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"], "duration_s": round(duration, 1)}


def library_sha256(library):
    """Stable hash of the ordered library: the identity checked against results/FROZEN.json."""
    return hashlib.sha256(json.dumps(normalize_library(library), sort_keys=True).encode()).hexdigest()


def run_round(condition, library, dev_prs, sel_prs, gated=True, run_id=None, rejected=(), min_gain=1, max_regress=1,
              salt=None, round_no=None):
    """dev_prs/sel_prs: instance dicts. Returns (new_library, record). Image cleanup belongs to the caller, once the
    round's baseline and candidate are both finished with Docker."""
    library = normalize_library(library)
    s0 = harness.spent()
    dev = evaluate(dev_prs, library, condition, salt=salt)
    dev_by_id = {r["instance_id"]: r for r in dev if r.get("instance_id")}  # keyed, never zipped with dev_prs
    inst_by_id = {i["instance_id"]: i for i in dev_prs}
    forbidden = []
    for iid, r in dev_by_id.items():
        if r.get("passed") is None or iid not in inst_by_id:
            continue
        forbidden += forbidden_for(inst_by_id[iid], r.get("findings") or [])[1]
    feedback = build_feedback(dev, library, condition, rejected)
    has_evidence = any(r.get("passed") is not None for r in dev)
    global LAST_OPT_RAW
    opt_err, LAST_OPT_RAW = None, None
    try:
        edits = propose_edits(library, feedback, condition, forbidden, round_no=round_no) if has_evidence else []
    except ValueError as e:
        # Sanitizer rejected the optimizer payload (e.g. the reviewer quoted gold text). Skip this round's
        # edit and record why; a safety check must never kill a multi-hour run.
        edits, opt_err = [], str(e)[:300]
    cand = apply_edits(library, edits)
    pr_records = [{k: r.get(k) for k in ("instance_id", "repo", "pr_type", "skill_ids", "findings", "passed", "error",
                                         "cost_usd", "usage", "duration_s")} for r in dev]
    rec = {"condition": condition, "round": round_no, "gated": gated, "edits": edits, "optimizer_error": opt_err,
           "optimizer_raw": LAST_OPT_RAW,
           "dev_passed": {r["instance_id"]: r["passed"] for r in dev if r.get("instance_id")},
           "dev": summarize(dev), "prs": pr_records,
           "library_before": [dict(s) for s in library],
           "library_sha256_before": library_sha256(library),
           "salt": salt, "harness_version": harness.HARNESS_VERSION,
           "model": reviewer.MODEL, "threshold": {"min_gain": min_gain, "max_regress": max_regress}}
    if not edits:
        rec.update(accept=False, gained=0, regressed=0, net=0, excluded=[],
                   note="optimizer payload rejected" if opt_err else "no edits")
        final = library
    elif not gated:
        rec.update(accept=True, gained=None, regressed=None, net=None, excluded=[], note="ungated: selection not evaluated")
        final = cand
    else:
        # ponytail: the (single) edit is the whole candidate; per-edit gating would need one selection pass per edit
        base = evaluate(sel_prs, library, condition, salt=salt)
        new = evaluate(sel_prs, cand, condition, salt=salt)
        rec.update(gate({r["instance_id"]: r["passed"] for r in base if r.get("instance_id")},
                        {r["instance_id"]: r["passed"] for r in new if r.get("instance_id")}, min_gain, max_regress))
        rec.update(sel_base=summarize(base), sel_new=summarize(new),
                   sel_base_passed={r["instance_id"]: r["passed"] for r in base if r.get("instance_id")},
                   sel_new_passed={r["instance_id"]: r["passed"] for r in new if r.get("instance_id")})
        final = cand if rec["accept"] else library
    rec["library_after"] = [dict(s) for s in final]
    rec["library_sha256_after"] = library_sha256(final)
    rec["run_id"] = run_id
    rec["cost_usd"] = round(harness.spent() - s0, 4)
    harness.append_json(ROOT / f"results/evolution/{condition}.jsonl", rec)
    return final, rec


# ---------- sampling ----------
def _code_sha():
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read git HEAD for FROZEN check: {(r.stderr or '').strip()[:200]}")
    return r.stdout.strip()


CODE_FILES = ("harness.py", "evolve.py", "reviewer.py", "data.py", "proxy_hooks.py")


def _code_unchanged_since(sha):
    """True iff `sha` is an ancestor of HEAD and no scored code file differs between them. Committing FROZEN.json itself
    moves HEAD, so the frozen commit may be HEAD's parent; any later change to CODE_FILES still blocks the test set."""
    if not sha:
        return False
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=str(ROOT), capture_output=True)
    if anc.returncode != 0:
        return False
    diff = subprocess.run(["git", "diff", "--quiet", sha, "HEAD", "--", *CODE_FILES], cwd=str(ROOT), capture_output=True)
    return diff.returncode == 0


def _code_manifest(root=None):
    """SHA-256 of every scored code file. HEAD alone is not enough: uncommitted edits must also block the test set."""
    root = Path(root or ROOT)
    return {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in CODE_FILES}


def _require_frozen():
    """The test partition may run only from the exact frozen code in results/FROZEN.json, and returns that record."""
    path = ROOT / "results/FROZEN.json"
    if not path.exists():
        raise RuntimeError("test partition requires results/FROZEN.json (code_sha + code_manifest) before it may be unlocked")
    frozen = json.loads(path.read_text())
    code_sha = str(frozen.get("code_sha") or "").strip()
    head = _code_sha()
    if code_sha != head and not _code_unchanged_since(code_sha):
        raise RuntimeError(f"FROZEN.json code_sha {code_sha[:12]} does not match git HEAD {head[:12]}, and the scored "
                           f"code files changed since it (or it is not an ancestor of HEAD)")
    manifest = frozen.get("code_manifest")
    if not isinstance(manifest, dict) or not manifest:
        raise RuntimeError("FROZEN.json is missing code_manifest (hashes of the scored code files)")
    bad = sorted(f for f, h in _code_manifest().items() if manifest.get(f) != h)
    if bad:
        raise RuntimeError(f"FROZEN.json code_manifest does not match the working tree: {bad}")
    hv = frozen.get("harness_version")
    if hv is not None and str(hv) != harness.HARNESS_VERSION:
        raise RuntimeError(f"FROZEN.json harness_version {hv} != running {harness.HARNESS_VERSION}")
    return frozen


def eligible(partition, allow_test=False):
    """({pr_type: [ids]}, excluded). Same filter for every condition: no docker image, or diff too long for the reviewer cap.
    The test partition raises unless allow_test, and when allowed requires a matching results/FROZEN.json."""
    if partition == "test":
        if not allow_test:
            raise ValueError("locked test partition: our system may not run on it")
        _require_frozen()
    return _eligible(partition)


def _eligible(partition):
    """Unguarded body of eligible(): reads split ids and PR inputs only, never outcomes. freeze.py uses it to record the
    test ids before the guard can pass."""
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


def sample(partition, n, seed, allow_test=False):
    """Seeded and nested: sample(p, m, seed) == sample(p, n, seed)[:m] for m < n. Each type's PRs are shuffled with the seed,
    then types are interleaved by largest deficit against their population share, so every prefix is proportionally
    stratified to within one PR. Returned in that order (not sorted)."""
    by, _ = eligible(partition, allow_test=allow_test)
    rng = random.Random(seed)
    pools = {t: rng.sample(by[t], len(by[t])) for t in sorted(by)}
    total, taken, out = sum(map(len, pools.values())), {t: 0 for t in pools}, []
    for k in range(min(n, total)):
        t = max((t for t in pools if taken[t] < len(pools[t])), key=lambda t: (len(pools[t]) * (k + 1) / total - taken[t], t))
        out.append(pools[t][taken[t]]); taken[t] += 1
    return out


def score_library(partition, condition, library=None, n=None, seed=0, salt=None, allow_test=False, allow_overwrite=False,
                  resume=False):
    """Score one frozen library on a partition. On test, FROZEN.json must pin the code manifest, the library hash and the
    exact test id list; the final output is written once. A stopped run is written to results/partial/ and can be
    completed with --resume using the same salt/library; partials never match the results/test_*.json glob."""
    if partition == "test" and not allow_test:
        raise ValueError("locked test partition: pass --unlock-test only after freezing")
    frozen = _require_frozen() if partition == "test" else None
    if condition == "static":
        lib = []
    elif library:
        lib = normalize_library(json.loads(Path(library).read_text()))
    else:
        _, lib = latest_snapshot(condition)
        if lib is None:
            raise SystemExit(f"no library snapshot for {condition}; pass --library")
    lib = normalize_library(lib)
    if condition != "static" and frozen is not None:
        want = (frozen.get("libraries") or {}).get(condition)
        got = library_sha256(lib)
        if want != got:
            raise RuntimeError(f"FROZEN.json library hash for {condition} is {str(want)[:12]}, snapshot is {got[:12]}")
    ids = sample(partition, n if n is not None else 10 ** 9, seed, allow_test=allow_test)
    if frozen is not None:
        frozen_ids = frozen.get("test_ids")
        if not isinstance(frozen_ids, list) or sorted(frozen_ids) != sorted(ids):
            raise RuntimeError("FROZEN.json test_ids do not match the eligible test partition")
    out_path = ROOT / f"results/{partition}_{condition}.json"
    partial_path = ROOT / f"results/partial/{partition}_{condition}.json"
    if out_path.exists():
        if not allow_overwrite:
            raise SystemExit(f"{out_path} already exists: one scoring pass only (use --allow-overwrite to deliberately re-run)")
        out_path.unlink()
    partial = None
    if partial_path.exists():
        if allow_overwrite:
            partial_path.unlink()
        elif not resume:
            raise SystemExit(f"{partial_path} exists: complete it with --resume (same --salt/--library) or remove it")
        else:
            partial = json.loads(partial_path.read_text())
            if partial.get("partition") != partition or partial.get("condition") != condition:
                raise SystemExit(f"{partial_path}: partition/condition mismatch; refusing --resume")
            if partial.get("library_sha256") != library_sha256(lib):
                raise SystemExit(f"{partial_path}: library hash differs from the current snapshot; refusing --resume")
            if sorted(partial.get("test_ids") or []) != sorted(ids):
                raise SystemExit(f"{partial_path}: test_ids differ from the current eligible set; refusing --resume")
            if salt and partial.get("salt") != salt:
                raise SystemExit(f"{partial_path}: salt {partial.get('salt')!r} != requested {salt!r}; refusing --resume")
            salt = salt or partial.get("salt")
            if not salt:
                raise SystemExit(f"{partial_path}: no recorded salt; cannot resume reproducibly")
    if not salt:
        if partition == "test":
            raise SystemExit("--salt is required for the locked test pass so an interrupted run resumes without re-rolling")
        salt = uuid.uuid4().hex
    res = [r for r in (partial or {}).get("prs", []) if r.get("instance_id") in set(ids)]
    done = {r["instance_id"] for r in res}
    missing = [i for i in ids if i not in done]
    inst = harness.instances()
    stopped = None
    try:
        if missing:
            evaluate([inst[i] for i in missing], lib, condition, res, salt=salt)
    except (harness.StopRun, KeyboardInterrupt) as ex:
        stopped = ex
        print("STOPPED:", repr(ex) if isinstance(ex, KeyboardInterrupt) else ex)
    order = {i: k for k, i in enumerate(ids)}
    res.sort(key=lambda r: order.get(r.get("instance_id"), len(order)))
    summary = summarize(res)
    incomplete = stopped is not None or len(res) < len(ids)
    out = {"condition": condition, "partition": partition, "seed": seed, "salt": salt,
           "n_requested": len(ids), "n_done": len(res), "partial": incomplete, "test_ids": ids,
           "library": lib, "library_sha256": library_sha256(lib),
           "code_sha": _code_sha(), "harness_version": harness.HARNESS_VERSION, "model": reviewer.MODEL,
           "excluded": {k: {"n": len(v), "ids": v} for k, v in eligible(partition, allow_test=allow_test)[1].items()},
           "summary": summary, "tokens": summary["tokens"], "duration_s": summary["duration_s"],
           "prs": res, "comments": comment_rows(res)}
    target = partial_path if incomplete else out_path
    harness.write_json(target, out)
    if not incomplete:
        partial_path.unlink(missing_ok=True)
    print(f"{condition} {partition}: {json.dumps(summary)} tokens={out['tokens']} -> {target}")
    if incomplete:
        print(f"INCOMPLETE ({len(res)}/{len(ids)}); resume with the same --salt/--library")
    return out


def latest_snapshot(condition):
    """(n, library) of the highest results/skills/<condition>/round_<n>.json, or (None, None)."""
    d = ROOT / f"results/skills/{condition}"
    best = None
    if d.exists():
        for p in d.iterdir():
            m = SNAP_RE.match(p.name)
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), p)
    return (None, None) if best is None else (best[0], json.loads(best[1].read_text()))


def rejected_from_log(condition, upto):
    """Rejected edits through round `upto`, so a resumed run keeps the do-not-repeat context. Reads every run in the log
    (a resume appends a new header), keeping the latest record per round number, as analyze_evolution does."""
    path = ROOT / f"results/evolution/{condition}.jsonl"
    if not path.exists():
        return []
    recs = []
    for line in path.read_text().splitlines():
        try:
            recs.append(json.loads(line))
        except Exception:
            continue
    by_round = {r["round"]: r for r in recs if r.get("round") is not None and "accept" in r}
    out = []
    for rn in sorted(by_round):
        r = by_round[rn]
        if rn <= upto and r.get("edits") and not r.get("accept"):
            out += [{**e, "outcome": "rejected"} for e in r["edits"]]
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
    sc = sub.add_parser("score")
    sc.add_argument("--partition", default="test")
    sc.add_argument("--condition", choices=["static", "global", "scoped"], required=True)
    sc.add_argument("--library", default=None)
    sc.add_argument("--n", type=int, default=None)
    for p in (s, e, sc):
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--gate-min-gain", type=int, default=1)  # required NET gain (gained - regressed)
        p.add_argument("--gate-max-regress", type=int, default=1)
        p.add_argument("--fresh", action="store_true")
        p.add_argument("--salt", default=None)  # deterministic fresh-draw key; required for the locked test pass
        p.add_argument("--resume", action="store_true")
        p.add_argument("--unlock-test", action="store_true")
        p.add_argument("--allow-overwrite", action="store_true")
        p.add_argument("--stop-after-rejections", type=int, default=2)
    a = ap.parse_args()
    if a.fresh and a.resume:
        raise SystemExit("--fresh and --resume are mutually exclusive: a resumed run must reuse its recorded draws")
    salt = a.salt or (uuid.uuid4().hex if a.fresh else None)
    harness.setup()
    harness.start_proxy()
    inst = harness.instances()
    try:
        if a.cmd == "score":
            score_library(a.partition, a.condition, a.library, a.n, a.seed, salt, a.unlock_test, a.allow_overwrite, a.resume)
        elif a.cmd == "static":
            path = ROOT / f"results/static_{a.partition}.json"
            if a.partition == "test" and path.exists() and not a.allow_overwrite:
                raise SystemExit(f"{path} already exists: one scoring pass only (use --allow-overwrite to deliberately re-run)")
            ids = sample(a.partition, a.n, a.seed, allow_test=a.unlock_test)
            res = []
            try:
                evaluate([inst[i] for i in ids], [], "static", res, salt=salt)
            except (harness.StopRun, KeyboardInterrupt) as ex:
                print("STOPPED:", repr(ex) if isinstance(ex, KeyboardInterrupt) else ex)
            summary = summarize(res)
            out = {"condition": "static", "partition": a.partition, "seed": a.seed, "salt": salt,
                   "n_requested": len(ids), "n_done": len(res),
                   "code_sha": _code_sha(), "harness_version": harness.HARNESS_VERSION, "model": reviewer.MODEL,
                   "excluded": {k: {"n": len(v), "ids": v} for k, v in eligible(a.partition, allow_test=a.unlock_test)[1].items()},
                   "summary": summary, "tokens": summary["tokens"], "duration_s": summary["duration_s"],
                   "prs": res, "comments": comment_rows(res), "cost_usd": round(sum(r["cost_usd"] for r in res), 4)}
            harness.write_json(path, out)
            print(f"static {a.partition}: {json.dumps(summary)} tokens={out['tokens']} -> {path}")
        else:
            run_id = uuid.uuid4().hex[:8]  # separates resumed runs' headers/aborted records in the append-only log
            dev_ids = sample("development", a.dev_n, a.seed, allow_test=a.unlock_test)
            sel_ids = sample("selection", a.sel_n, a.seed, allow_test=a.unlock_test)
            dev, sel = [inst[i] for i in dev_ids], [inst[i] for i in sel_ids]
            snap_n, snap = latest_snapshot(a.condition) if a.resume else (None, None)
            if snap is not None:
                lib, start = normalize_library(snap), snap_n + 1
                rejected = rejected_from_log(a.condition, snap_n)
            else:
                lib, start, rejected = [], 1, []  # round_0 is the empty starting snapshot, not a round
            harness.append_json(ROOT / f"results/evolution/{a.condition}.jsonl", {
                "run_id": run_id, "condition": a.condition, "seed": a.seed, "gated": not a.no_gate,
                "dev_ids": dev_ids, "sel_ids": sel_ids, "resumed_from": snap_n, "salt": salt,
                "gate": {"min_gain": a.gate_min_gain, "max_regress": a.gate_max_regress},
                "excluded": {p: {k: len(v) for k, v in eligible(p)[1].items()} for p in ("development", "selection")}})
            if snap is None:
                save_snapshot(a.condition, 0, lib)  # a resumed run never overwrites an earlier snapshot
            ran, consecutive = max(0, start - 1), 0
            try:
                for r in range(start, a.rounds + 1):
                    lib, rec = run_round(a.condition, lib, dev, sel, not a.no_gate, run_id, rejected,
                                         a.gate_min_gain, a.gate_max_regress, salt, r)
                    save_snapshot(a.condition, r, lib)
                    harness.cleanup_images()  # round is over; both selection evaluations are done with Docker
                    ran = r
                    if rec["accept"]:
                        consecutive = 0
                    else:  # PROTOCOL §8: an empty edit list is recorded as a rejection, so it counts toward the stop rule
                        rejected += [{**ed, "outcome": "rejected"} for ed in rec["edits"]]
                        consecutive += 1
                    print(f"round {r}: accept={rec['accept']} gained={rec['gained']} regressed={rec['regressed']} "
                          f"net={rec['net']} dev errors-as-failed={rec['dev']['pass_rate_errors_as_failed']} "
                          f"n_errors={rec['dev']['n_errors']} cost=${rec['cost_usd']}")
                    if a.stop_after_rejections > 0 and consecutive >= a.stop_after_rejections:
                        harness.append_json(ROOT / f"results/evolution/{a.condition}.jsonl",
                                            {"run_id": run_id, "condition": a.condition, "stopped": "stop-after-rejections",
                                             "round": r, "consecutive_rejections": consecutive})
                        print(f"stop-after-rejections: {consecutive} consecutive rejected edits at round {r}")
                        break
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
