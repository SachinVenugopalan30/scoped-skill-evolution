import json
from collections import Counter
import data

rows = data.load(data.STAGES[-1])
splits = json.load(open(data.ROOT / "results/splits.json"))


def test_census():
    c = data.census()
    assert c["stage0_full_test_split"] == {"instances": 671, "comments": 1313}
    assert c["stage1_comment_filter"] == {"instances": 410, "comments": 595}
    assert c["stage2_docker_image"] == {"instances": 410, "comments": 595}
    assert c["stage3_testgen_verified"] == {"instances": 339, "comments": 485}
    assert c["stage4_agent_resolved"] == {"instances": 184, "comments": 234}
    assert c["stage4_repos"] == 67
    assert Counter(data.pr_type(r) for r in rows) == {"bug_fix": 67, "feature": 68, "other": 49}


def test_splits():
    ids = splits["instance_ids"]
    allids = [i for p in ids.values() for i in p]
    assert len(allids) == 184 and set(allids) == {r["instance_id"] for r in rows}
    repo_of = {r["instance_id"]: r["repo"] for r in rows}
    repos = {p: {repo_of[i] for i in v} for p, v in ids.items()}
    assert not (repos["development"] & repos["selection"] or repos["development"] & repos["test"]
                or repos["selection"] & repos["test"])
    for p, (n, c, r) in data.TARGETS.items():
        s = splits["summary"][p]
        assert (s["prs"], s["comments"], s["repos"]) == (n, c, r) and len(s["types"]) == 3


def test_reproducible():
    again = data.make_splits(rows, splits["seed"])
    assert again["seed"] == splits["seed"] and again["instance_ids"] == splits["instance_ids"]
    assert again["repo_assignment"] == splits["repo_assignment"]


# ---------------- Task 2: offline tests (no network, no Docker, no LLM) ----------------
import os, socket, sys, subprocess, tempfile, threading, time, warnings
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import litellm
import analyze, evolve, harness, reviewer


def _boom(*a, **k):
    raise AssertionError("network/Docker/LLM call attempted in an offline test")


subprocess.run = subprocess.Popen = litellm.completion = socket.create_connection = _boom
socket.socket.connect = _boom

INST = harness.instances()["All-Hands-AI__OpenHands-8343@9d4e2af"]
KEY = "sk-ant-test-SECRETKEY123"


@contextmanager
def tmp_state():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(harness, "LEDGER", Path(d) / "ledger.jsonl"), \
            mock.patch.object(harness, "CACHE", Path(d) / "cache"), mock.patch.object(harness, "RAW", Path(d) / "raw"), \
            mock.patch.object(harness, "get_key", lambda: KEY):
        yield Path(d)


def fake_resp(text, pt=1000, ct=100):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct))


GOOD = '[{"file": "a.py", "line": 3, "issue_header": "h", "issue_content": "c"}]'


def test_gate():
    g = evolve.gate
    assert g({"a": False}, {"a": True}) == {"accept": True, "gained": 1, "regressed": 0, "net": 1, "excluded": []}
    assert not g({"a": False, "b": True}, {"a": False, "b": True})["accept"]
    assert g({"a": False, "b": False, "c": True, "d": False}, {"a": True, "b": True, "c": False, "d": False})["accept"]  # 2/1
    assert not g({"a": False, "b": False, "c": True, "d": True}, {"a": True, "b": True, "c": False, "d": False})["accept"]  # 2/2
    r = g({"a": False, "b": True}, {"a": True, "b": None})
    assert r["regressed"] == 0 and r["excluded"] == ["b"] and r["accept"] and r["net"] == 1
    assert g({"a": None}, {"a": True})["gained"] == 0
    # net is always gained - regressed, and errored PRs are excluded from both counts
    r = g({"a": False, "b": False, "c": True}, {"a": True, "b": False, "c": None})
    assert (r["gained"], r["regressed"], r["net"], r["excluded"]) == (1, 0, 1, ["c"])
    # thresholds: NET gain (gained - regressed) must reach min_gain, and regressions stay at or below max_regress
    base = {"a": False, "b": True, "c": False}
    cand = {"a": True, "b": True, "c": False}
    r = g(base, cand, min_gain=2)
    assert (r["gained"], r["regressed"], r["net"]) == (1, 0, 1) and not r["accept"]
    assert g(base, cand, min_gain=1)["accept"]
    # one gain and one regression is net zero, so it must not pass a min_gain=1 gate even though gained == 1
    tie_base = {"a": False, "b": True}
    tie_cand = {"a": True, "b": False}
    r = g(tie_base, tie_cand, min_gain=1, max_regress=1)
    assert (r["gained"], r["regressed"], r["net"]) == (1, 1, 0) and not r["accept"]
    # two gains and one regression (net +1): tolerated only when max_regress allows the regression
    swap_base = {"a": True, "b": False, "c": False}
    swap_cand = {"a": False, "b": True, "c": True}
    r = g(swap_base, swap_cand)
    assert (r["gained"], r["regressed"], r["net"]) == (2, 1, 1)
    assert not g(swap_base, swap_cand, max_regress=0)["accept"]
    assert g(swap_base, swap_cand, max_regress=1)["accept"]
    assert not g({}, {})["accept"] and g({}, {})["net"] == 0
    rep = {"error": None, "per_comment": [{"comment_index": 0, "passed": True}, {"comment_index": 1, "passed": False}]}
    assert harness.pr_passed(rep) is False
    assert harness.pr_passed({**rep, "error": "x"}) is None


PLANT = dict(path="src/secret_mod/handler.py", name="handler.py", test="def test_planted_zzz(): assert frobnicate(42)",
             gold="Please rename the frobnicate variable", repo="acme/widgets", iid="acme__widgets-77@abc1234")


def test_sanitizer():
    findings = [{"file": PLANT["path"], "line": 9, "issue_header": f"Bug in {PLANT['name']} of widgets",
                 "issue_content": f"See {PLANT['path']} line 12 and {PLANT['iid']} in {PLANT['repo']}: unchecked input"}]
    forbid = [PLANT["repo"], "widgets", PLANT["iid"], PLANT["path"], PLANT["name"]]
    fb = evolve.sanitize_feedback("bug_fix", findings, [{"comment_index": 0, "passed": True}, {"comment_index": 1, "passed": False}], forbid)
    assert set(fb) == {"pr_type", "findings", "passed", "total"} and (fb["passed"], fb["total"]) == (1, 2)
    assert set(fb["findings"][0]) == {"issue_header", "issue_content"}
    un = evolve.sanitize_feedback("other", [{"issue_header": "x", "issue_content": "edit other/unlisted/mod.py and util.js line 5-9"}], [])
    assert "unlisted" not in json.dumps(un) and "util.js" not in json.dumps(un) and "5-9" not in json.dumps(un)
    bad = [PLANT["path"], PLANT["name"], PLANT["test"], PLANT["gold"], PLANT["repo"], PLANT["iid"], "secret_mod", "line 12"]
    s = json.dumps(fb)
    for b in bad:
        assert b not in s, b
    evolve.assert_clean(fb, bad)
    for b in bad[:6]:  # planted strings must trip assert_clean
        for payload in (json.dumps({"x": f"prefix {b} suffix"}), {"x": b}):
            try:
                evolve.assert_clean(payload, [b]); raise SystemExit(f"assert_clean missed {b}")
            except ValueError:
                pass
    try:  # multi-line gold text is JSON-escaped in the payload; must still be caught
        evolve.assert_clean([{"content": "a\nb"}], ["a\nb"]); raise SystemExit("missed multiline")
    except ValueError:
        pass


def test_retrieval():
    lib = [{"id": "s1", "scope": "global", "text": "check inputs"}, {"id": "s2", "scope": "bug_fix", "text": "check null"},
           {"id": "s3", "scope": "feature", "text": "check docs"}, {"id": "s4", "scope": "global", "text": "check errors"}]
    ids = lambda c, t, b=400, l=lib: [s["id"] for s in evolve.retrieve(l, t, c, b)]
    # type-scoped first, then global; newest-first within each group (legacy list position = order)
    assert ids("scoped", "bug_fix") == ["s2", "s4", "s1"]
    assert ids("scoped", "other") == ["s4", "s1"]
    assert ids("static", "bug_fix") == []
    assert ids("global", "bug_fix") == ["s4", "s1"]  # global condition never sees non-global
    # explicit orders: newest (highest order) first inside the type group, then inside global
    ordered = [{"id": "g_old", "scope": "global", "text": "g old", "order": 1},
               {"id": "g_new", "scope": "global", "text": "g new", "order": 9},
               {"id": "b_old", "scope": "bug_fix", "text": "b old", "order": 2},
               {"id": "b_new", "scope": "bug_fix", "text": "b new", "order": 8}]
    assert [s["id"] for s in evolve.retrieve(ordered, "bug_fix", "scoped")] == ["b_new", "b_old", "g_new", "g_old"]
    # hard cap on retrieved skills, newest first
    many = [{"id": f"g{i}", "scope": "global", "text": f"skill {i}", "order": i} for i in range(1, 11)]
    got = evolve.retrieve(many, "bug_fix", "scoped")
    assert evolve.MAX_SKILLS == 6 and [s["id"] for s in got] == ["g10", "g9", "g8", "g7", "g6", "g5"]
    # dedup: normalized duplicate texts are injected once, at the position of the newest copy
    dup = [{"id": "a", "scope": "global", "text": "Check Inputs", "order": 1},
           {"id": "b", "scope": "global", "text": "  check   INPUTS ", "order": 4},
           {"id": "c", "scope": "global", "text": "other", "order": 3}]
    assert [s["id"] for s in evolve.retrieve(dup, "bug_fix", "scoped")] == ["b", "c"]
    # just-added skills are newest and therefore retrieved first
    added = evolve.apply_edits([], [{"op": "add", "scope": "bug_fix", "text": "fresh guidance"}])
    assert added[0]["order"] == 1 and [s["id"] for s in evolve.retrieve(added, "bug_fix", "scoped")] == ["s1"]
    added = evolve.apply_edits(added, [{"op": "add", "scope": "bug_fix", "text": "newer guidance"}])
    assert [s["id"] for s in evolve.retrieve(added, "bug_fix", "scoped")] == ["s2", "s1"]
    tok = lambda t: litellm.token_counter(model=reviewer.MODEL, text=t)
    # the default budget is 400 tokens of injected skill text
    assert evolve.RETRIEVAL_BUDGET == 400
    assert tok("\n".join(s["text"] for s in got)) <= evolve.RETRIEVAL_BUDGET
    big = {"id": "g1", "scope": "global", "text": "word " * 60}
    small = {"id": "g2", "scope": "global", "text": "tiny"}
    typed = {"id": "t1", "scope": "bug_fix", "text": "typed skill"}
    budget = tok("\n" + typed["text"] + "\n" + small["text"]) + 2
    assert tok(big["text"]) > budget
    got = evolve.retrieve([big, small, typed], "bug_fix", "scoped", budget)
    assert [s["id"] for s in got] == ["t1", "g2"]  # skip-and-continue: oversize skill skipped, later small one kept
    assert tok("\n".join(s["text"] for s in got)) <= budget
    assert ids("global", "bug_fix", budget, [big, small]) == ["g2"]


def token_prices(f):
    """Cost = output_tokens / 10 dollars, so fake Claude JSON can carry exact costs (4 tokens = $0.40)."""
    import functools
    @functools.wraps(f)
    def g(*a, **k):
        with mock.patch.object(reviewer, "PRICE_IN", 0.0), mock.patch.object(reviewer, "PRICE_OUT", 1e5):
            return f(*a, **k)
    return g


@token_prices
def test_ledger():
    with tmp_state():
        with mock.patch.object(harness, "CAP_USD", 1.0):
            harness.record("review", "x", 0.6)
            harness.check_budget(0.4)
            try:
                harness.check_budget(0.41); raise SystemExit("cap not enforced")
            except harness.BudgetExceeded:
                pass
        assert abs(harness.spent() - 0.6) < 1e-9  # recomputed from disk
        harness.record("repair", "x", None)
        assert abs(harness.spent() - 0.6 - harness.MAX_REPAIR_USD) < 1e-9  # unknown cost is the max, not 0
        harness.record("repair", "x", float("nan"))
        assert abs(harness.spent() - 0.6 - 2 * harness.MAX_REPAIR_USD) < 1e-9
        # unparseable / timed-out claude output is charged at the max
        g = harness.parse_claude("garbage")
        assert g["cost"] == harness.MAX_REPAIR_USD and not g["ok"]
        p = harness.parse_claude('{"type":"result","result":"hi","usage":{"input_tokens":9,"cache_read_input_tokens":9,"output_tokens":2},"is_error":true,"subtype":"x"}')
        assert (p["text"], p["cost"], p["ok"], p["is_error"], p["subtype"]) == ("hi", 0.2, True, True, "x")
        assert not harness.parse_claude('{"result":"no cost"}')["ok"]
        # corrupt trailing ledger line is counted conservatively, not a crash
        before = harness.spent()
        with harness.LEDGER.open("a") as f:
            f.write('{"kind": "repair", "inst')
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert abs(harness.spent() - before - harness.MAX_REPAIR_USD) < 1e-9 and w
    with tmp_state():
        ts = [threading.Thread(target=lambda: [harness.record("k", "i", 0.01) for _ in range(50)]) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        lines = harness.LEDGER.read_text().splitlines()
        assert len(lines) == 400 and all(json.loads(l)["cost"] == 0.01 for l in lines)
    # writes happen only while the lock is held
    class Spy:
        held = False
        def __enter__(self): Spy.held = True
        def __exit__(self, *a): Spy.held = False
    real_open = Path.open
    def chk(self, *a, **k):
        assert Spy.held, "ledger write without lock"
        return real_open(self, *a, **k)
    with tmp_state(), mock.patch.object(harness, "_lock", Spy()), mock.patch.object(Path, "open", chk):
        harness.record("k", "i", 0.01)


def test_reviewer():
    many = json.dumps([{"file": f"f{i}.py", "line": i + 1, "issue_header": "h", "issue_content": "c"} for i in range(8)])
    assert len(reviewer.parse_findings("```json\n" + many + "\n```")) == 5
    mixed = json.dumps([{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"},
                        {"file": "a.py", "issue_header": "h", "issue_content": "c"},            # no location
                        {"file": "", "line": 1, "issue_header": "h", "issue_content": "c"},    # empty file
                        {"file": "a.py", "start_line": 2, "end_line": 4, "issue_header": "h", "issue_content": "c"},
                        "junk", {"file": "a.py", "line": "7", "issue_header": "h", "issue_content": "c"}])
    got = reviewer.parse_findings(mixed)
    assert [("line" in g, "start_line" in g) for g in got] == [(True, False), (False, True)]
    assert reviewer.parse_findings("no json here") is None
    # git-diff metadata is stripped for the WAF fallback; file name and hunks stay
    raw = "diff --git a/pkg/mod.py b/pkg/mod.py\nindex aaa..bbb 100644\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,2 +1,2 @@\n-x\n+y"
    cleaned = reviewer.clean_diff(raw)
    assert "### File: pkg/mod.py" in cleaned and "diff --git" not in cleaned
    assert "index " not in cleaned and "--- " not in cleaned and "+++ " not in cleaned
    assert "@@ -1,2 +1,2 @@" in cleaned and "+y" in cleaned
    # prompt has no gold data
    msgs = json.dumps(reviewer.build_messages(INST, ["skill one"]))
    gold = [c["text"] for c in INST["reference_review_comments"]] + [INST["merged_patch"], INST["hints_text"]] + \
           [e["test_code"] for _, _, e in harness.stage4_tests(INST)]
    for g in gold:
        assert json.dumps(g)[1:-1] not in msgs
    assert INST["title"] in msgs and "skill one" in msgs
    # retry once on unparseable, then error with cost recorded; cost never 0
    calls = []
    def fake(**kw):
        calls.append(kw); return fake_resp("sorry no json")
    with mock.patch.object(litellm, "completion", fake), mock.patch.object(litellm, "completion_cost", side_effect=Exception("no price")):
        r = reviewer.review(INST, [])
    assert len(calls) == 2 and r["findings"] == [] and r["error"] and abs(r["cost_usd"] - 2 * (1000 * reviewer.PRICE_IN + 100 * reviewer.PRICE_OUT) / 1e6) < 1e-12
    assert calls[0]["temperature"] == 0
    # C2b: the retry is a different request (more tokens, stricter instruction)
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"] and calls[1]["messages"] != calls[0]["messages"]
    assert "valid JSON only" in calls[1]["messages"][-1]["content"]
    # C2a: spend recorded per attempt, so a raised 2nd attempt cannot lose the 1st attempt's cost
    seen = []
    def second_raises(**kw):
        if seen: raise RuntimeError("api down")
        seen.append(1); return fake_resp("nope")
    with tmp_state(), mock.patch.object(litellm, "completion", second_raises), mock.patch.object(litellm, "completion_cost", return_value=0.03):
        try:
            harness.run_review(INST, ["q"]); raise SystemExit("should raise")
        except RuntimeError:
            pass
        assert abs(harness.spent() - 0.03) < 1e-12
    # truncated outer list -> None (retry), never a stray inner list
    assert reviewer.extract_list('[{"file": "a", "x": [1, 2], "issue_header": "h"') is None
    # a list wrapped under an unexpected key is used only when it is the object's sole list
    assert reviewer.extract_list('{"library": [{"op": "add"}]}', "edits") == [{"op": "add"}]
    assert reviewer.extract_list('{"a": [1], "b": [2]}', "edits") is None
    assert evolve.parse_edits('{\n "library": [{"op": "add", "scope": "global", "text": "t"}]}', [], "scoped") == \
        [{"op": "add", "scope": "global", "text": "t"}]
    assert reviewer.parse_findings('[{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c", "w": [1]},{"fi') is None
    seq = iter([fake_resp("nope"), fake_resp(GOOD)])
    with mock.patch.object(litellm, "completion", lambda **kw: next(seq)), mock.patch.object(litellm, "completion_cost", return_value=0.01):
        r = reviewer.review(INST, [])
    assert len(r["findings"]) == 1 and "error" not in r and r["cost_usd"] == 0.02


def test_review_waf_fallback():
    """A 403 ASU WAF block retries once with the git-diff metadata stripped, then succeeds."""
    calls = []
    def fake(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise litellm.exceptions.APIError(403, "ASU error page 403", "openai", "openai/qwen36-27b")
        return fake_resp(GOOD)
    with mock.patch.object(litellm, "completion", fake):
        r = reviewer.review(INST, [])
    assert len(calls) == 2 and r.get("error") is None and len(r["findings"]) == 1
    first, second = calls[0]["messages"][-1]["content"], calls[1]["messages"][-1]["content"]
    if "diff --git" in first:
        assert "### File:" in second and "diff --git" not in second
    # a non-403 APIError is not retried
    calls.clear()
    def auth(**kw):
        calls.append(kw)
        raise litellm.exceptions.APIError(401, "401 invalid key", "openai", "openai/qwen36-27b")
    with mock.patch.object(litellm, "completion", auth):
        try:
            reviewer.review(INST, []); raise SystemExit("401 was retried")
        except litellm.exceptions.APIError:
            pass
    assert len(calls) == 1


def test_cache():
    with tmp_state():
        n = []
        def rev(**kw):
            n.append(1); return fake_resp(GOOD)
        with mock.patch.object(litellm, "completion", rev), mock.patch.object(litellm, "completion_cost", return_value=0.05):
            a = harness.run_review(INST, ["s"])
            s1 = harness.spent()
            b = harness.run_review(INST, ["s"])
            assert len(n) == 1 and b["cost_usd"] == 0 and b["cached"] and b["findings"] == a["findings"] and harness.spent() == s1
            harness.run_review(INST, ["t"])  # different skills -> different key
            assert len(n) == 2
            with mock.patch.object(harness, "CAP_USD", 0.0):  # a hit bypasses the budget check
                assert harness.run_review(INST, ["s"])["cached"]
        # C2c: an errored review that cost money IS cached; re-evaluating reuses it at $0
        n.clear()
        with mock.patch.object(litellm, "completion", lambda **kw: n.append(1) or fake_resp("junk")), mock.patch.object(litellm, "completion_cost", return_value=0.01):
            e = harness.run_review(INST, ["err"])
            spent_after = harness.spent()
            e2 = harness.run_review(INST, ["err"])
        assert e["error"] and len(n) == 2 and e2["error"] and e2["cached"] and e2["cost_usd"] == 0 and harness.spent() == spent_after
        assert spent_after > s1
        # empty findings: no repair run, no container, zero cost, all failed
        rep = harness.repair_pr(INST, [])
        assert rep["cost_usd"] == 0 and rep["error"] is None and all(p["passed"] is False for p in rep["per_comment"])
        # repair cache. billed error (cost>0, incl. unknown-cost max) is cached; unbilled infra error retries once, uncached
        f = [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
        calls = []
        ok = lambda: [{"comment_index": 0, "passed": True}]
        def billed_err(inst, findings, model):
            # a billed error (cost > 0) is cached; MAX_REPAIR_USD is 0 under the free model, so use a real cost
            calls.append(1); return {"per_comment": ok(), "cost_usd": 0.01, "error": "claude timed out"}
        with mock.patch.object(harness, "run_repair", billed_err):
            r1 = harness.repair_pr(INST, f); sp = harness.spent(); r2 = harness.repair_pr(INST, f)
        assert len(calls) == 1 and r2["cached"] and r2["error"] and r2["cost_usd"] == 0 and harness.spent() == sp
        calls.clear()
        f2 = [{"file": "b.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
        def unbilled_err(inst, findings, model):
            calls.append(1); return {"per_comment": ok(), "cost_usd": 0.0, "error": "docker cp failed"}
        with mock.patch.object(harness, "run_repair", unbilled_err):
            harness.repair_pr(INST, f2)
            assert len(calls) == 2  # retried once
            harness.repair_pr(INST, f2)
            assert len(calls) == 4  # and not cached
        # cache key covers the rendered prompt, harness version and spend cap
        f = [{"file": "c.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
        calls.clear()
        def good(inst, findings, model):
            calls.append(1); return {"per_comment": ok(), "cost_usd": 0.5, "error": None}
        with mock.patch.object(harness, "run_repair", good):
            harness.repair_pr(INST, f); harness.repair_pr(INST, f)
            assert len(calls) == 1
            with mock.patch.object(harness, "HARNESS_VERSION", "other"):
                harness.repair_pr(INST, f)
            assert len(calls) == 2
            with mock.patch.object(harness, "MAX_REPAIR_USD", 1.0):
                harness.repair_pr(INST, f)
            assert len(calls) == 3
    # budget is checked before a paid call
    with tmp_state(), mock.patch.object(harness, "CAP_USD", 0.0), mock.patch.object(litellm, "completion", _boom):
        try:
            harness.run_review(INST, []); raise SystemExit("no budget check")
        except harness.BudgetExceeded:
            pass


def test_sample_scrub_optimizer():
    try:
        evolve.sample("test", 5, 0); raise SystemExit("sample allowed test partition")
    except ValueError:
        pass
    a = evolve.sample("selection", 12, 3)
    assert a == evolve.sample("selection", 12, 3) and len(a) == 12 and sorted(a) != sorted(evolve.sample("selection", 12, 4))
    assert not set(a) & harness.NO_IMAGE
    inst = harness.instances()
    dev_all = splits["instance_ids"]["development"]
    big = evolve.sample("development", len(dev_all), 0)
    by, ex = evolve.eligible("development")
    assert sorted(big) == sorted(i for v in by.values() for i in v) and len(big) + len(ex["no_image"]) + len(ex["oversize"]) == len(dev_all)
    assert all(len(inst[i]["commit_to_review"]["patch_to_review"]) <= reviewer.MAX_DIFF_CHARS for i in big)
    assert all(len(inst[i]["commit_to_review"]["patch_to_review"]) > reviewer.MAX_DIFF_CHARS for i in ex["oversize"])
    for part in ("development", "selection"):  # nested: smaller samples are prefixes of larger ones
        full = evolve.sample(part, 40, 5)
        assert all(evolve.sample(part, m, 5) == full[:m] for m in (1, 3, 7, 20, 39))
    ts = Counter(data.pr_type(inst[i]) for i in evolve.sample("development", 30, 0))
    assert len(ts) == 3 and sum(ts.values()) == 30
    # scrub
    assert KEY not in harness.scrub(f"x {KEY} y", KEY) and "x" in harness.scrub(f"x {KEY}", KEY)
    with tmp_state():
        harness.write_json(harness.CACHE / "t.json", {"diff": f"token={KEY}"})
        assert KEY not in (harness.CACHE / "t.json").read_text()
    # optimizer validation
    lib = [{"id": "s1", "scope": "global", "text": "old"}]
    raw = json.dumps([{"op": "add", "scope": "bug_fix", "text": "w " * 100}, {"op": "replace", "id": "nope", "text": "x"},
                      {"op": "delete", "id": "s1"}, {"op": "add", "scope": "feature", "text": "third"}, {"op": "zap"}])
    assert evolve.MAX_EDITS == 1
    ed = evolve.parse_edits(raw, lib, "scoped")
    assert len(ed) == 1 and ed[0]["scope"] == "bug_fix" and len(ed[0]["text"].split()) == 60
    assert all(e["scope"] == "global" for e in evolve.parse_edits(raw, lib, "global") if e["op"] == "add")
    assert evolve.apply_edits(lib, ed) == [{"id": "s1", "scope": "global", "text": "old", "order": 0},
                                           {"id": "s2", "scope": "bug_fix", "text": ed[0]["text"], "order": 1}]
    # propose_edits: asserts clean right before the call, records cost, refuses forbidden content
    with tmp_state(), mock.patch.object(litellm, "completion", lambda **kw: fake_resp(raw)), mock.patch.object(litellm, "completion_cost", return_value=0.02):
        out = evolve.propose_edits(lib, [{"pr_type": "other", "findings": [], "passed": 0, "total": 1}], "global", ["LEAKME"])
        assert len(out) == 1 and harness.spent() == 0.02
        assert evolve.LAST_OPT_RAW == raw  # the raw reply is kept for the round record, even when edits are dropped
        try:
            evolve.propose_edits(lib, [{"x": "LEAKME"}], "global", ["LEAKME"]); raise SystemExit("leak allowed")
        except ValueError:
            pass


def test_stats():
    rng = np.random.default_rng(1)
    rows = [{"repo": f"r{r}", "passed": bool(rng.random() < 0.4)} for r in range(60) for _ in range(3)]
    ci = analyze.bootstrap_ci(rows, 2000, 0)
    assert ci["lo"] <= ci["rate"] <= ci["hi"] and ci["lo"] <= 0.4 <= ci["hi"]
    mk = lambda p: [{"instance_id": f"i{r}", "comment_index": c, "repo": f"r{r}", "passed": p(r, c)} for r in range(40) for c in range(2)]
    same = mk(lambda r, c: (r + c) % 3 == 0)
    assert analyze.paired_permutation(same, same, 2000, 0) > 0.99
    assert analyze.paired_permutation(mk(lambda r, c: True), mk(lambda r, c: False), 2000, 0) < 0.01


def test_round():
    """run_round end to end with fake review/repair/optimizer: gate keeps a helpful edit, discards a harmful one."""
    ids = evolve.sample("selection", 4, 0)[:3]
    insts = [harness.instances()[i] for i in ids]
    seen = []
    def fake_review(inst, skills, model=None):
        return {"findings": [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}], "cost_usd": 0.0,
                "skills": list(skills)}
    def fake_repair(inst, findings, model=None):
        return {"per_comment": [{"comment_index": 0, "passed": "HELP" in seen[-1]}], "cost_usd": 0.0, "error": None}
    def rv(inst, skills, model=None):
        seen.append(" ".join(skills)); return fake_review(inst, skills)
    def run(edit_text, gated=True):
        edits = json.dumps([{"op": "add", "scope": "global", "text": edit_text}])
        with tmp_state(), mock.patch.object(evolve, "ROOT", Path(tempfile.mkdtemp())), mock.patch.object(harness, "run_review", rv), \
                mock.patch.object(harness, "repair_pr", fake_repair), mock.patch.object(litellm, "completion", lambda **kw: fake_resp(edits)), \
                mock.patch.object(litellm, "completion_cost", return_value=0.01):
            return evolve.run_round("scoped", [], insts, insts, gated)
    lib, rec = run("HELP always")
    assert "run_id" in rec
    assert rec["accept"] and rec["gained"] == 3 and len(lib) == 1
    lib, rec = run("unhelpful")
    assert not rec["accept"] and lib == [] and rec["gained"] == 0
    lib, rec = run("unhelpful", gated=False)
    assert rec["accept"] and len(lib) == 1


class FakeSession:
    """Stands in for DockerContainerSession: no Docker."""
    def __init__(self, diff="", *a, **k):
        self.diff, self.removed = diff, False
    def start(self): pass
    def remove(self, force=True): self.removed = True
    def run_command(self, cmd, **k):
        c = cmd if isinstance(cmd, str) else " ".join(cmd)
        out = self.diff if c == "git diff" else "2.1.0 (Claude Code)" if "--version" in c else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="", timed_out=False)


def repair_with(claude_json, rc=0, diff="", passed=True, stderr=""):
    sess = FakeSession(diff)
    out = SimpleNamespace(stdout=claude_json, stderr=stderr, returncode=rc, timed_out=False)
    with mock.patch.object(harness, "DockerContainerSession", lambda *a, **k: sess), mock.patch.object(harness, "ensure_image", lambda i: i), \
            mock.patch.object(harness, "setup_claude_in_container", lambda s: None), mock.patch.object(harness, "invoke_claude", lambda *a: out), \
            mock.patch.object(harness, "verify_with_test", lambda *a: (passed, "")):
        try:
            return harness.run_repair(INST, [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]), sess
        finally:
            pass


@token_prices
def test_repair_failures():
    cj = lambda **kw: json.dumps({"type": "result", "usage": {"input_tokens": 50, "output_tokens": 4}, **kw})
    with tmp_state():
        # C1: failed claude run with no diff is an infra error (passed None), not a failed test; cost kept
        r, sess = repair_with(cj(is_error=True, subtype="error_during_execution", result="boom"), rc=1)
        assert r["error"] and all(p["passed"] is None for p in r["per_comment"]) and r["cost_usd"] == 0.4 and sess.removed
        assert harness.pr_passed(r) is None
        # nonzero rc, unparseable output: error, charged at the max
        r, _ = repair_with("not json", rc=1)
        assert r["error"] and r["cost_usd"] == harness.MAX_REPAIR_USD and all(p["passed"] is None for p in r["per_comment"])
        # budget-stop subtype is legitimate: the diff that exists is scored
        r, _ = repair_with(cj(is_error=True, subtype="error_max_budget_usd", result="budget"), rc=1, diff="diff --git a b", passed=False)
        assert r["error"] is None and {p["passed"] for p in r["per_comment"]} == {False}
        r, _ = repair_with(cj(is_error=True, subtype="error_max_budget_usd"), rc=1, diff="", passed=True)
        assert r["error"] is None and {p["passed"] for p in r["per_comment"]} == {False}  # nothing changed: failed, not error
        # success with a diff is scored by the tests
        r, _ = repair_with(cj(is_error=False, subtype="success", result="ok"), diff="diff --git a b", passed=True)
        assert r["error"] is None and {p["passed"] for p in r["per_comment"]} == {True} and r["claude_version"].startswith("2.1")
        assert list(harness.RAW.glob("*.json"))  # raw output kept
        # auth / credit: fatal, stops the run at once, no phantom charge, no secret in the message.
        # Rate limits are transient (see test_rate_limit_retry), so they are not in this list.
        for msg in ("Invalid API key · Please run /login", "Credit balance is too low"):
            spent0 = harness.spent()
            try:
                with mock.patch.object(harness, "CACHE", harness.CACHE / msg[:5]):
                    sess = FakeSession()
                    out = SimpleNamespace(stdout=cj(is_error=True, result=f"{msg} {KEY}"), stderr="", returncode=1, timed_out=False)
                    with mock.patch.object(harness, "DockerContainerSession", lambda *a, **k: sess), mock.patch.object(harness, "ensure_image", lambda i: i), \
                            mock.patch.object(harness, "setup_claude_in_container", lambda s: None), mock.patch.object(harness, "invoke_claude", lambda *a: out):
                        harness.repair_pr(INST, [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}])
                raise SystemExit("fatal error not raised: " + msg)
            except harness.FatalRunError as e:
                assert KEY not in str(e) and abs(harness.spent() - spent0 - 0.4) < 1e-9 and sess.removed  # parsed cost is recorded before stopping
        assert KEY not in "".join(p.read_text() for p in harness.RAW.glob("*.json"))
        # a 403 unrecognized_model is an error result flagged transient (repair_pr retries it), not a fatal stop
        r, _ = repair_with(cj(is_error=True, subtype="error_during_execution",
                              result="Failed to authenticate. API Error: 403 ASU error page success [claude-code:unrecognized_model]"), rc=1)
        assert r["error"] and r["transient"] and all(p["passed"] is None for p in r["per_comment"])
        # fatal-looking text is not fatal when: budget-stop subtype, or prose merely mentioning authentication/billing
        r, _ = repair_with(cj(is_error=True, subtype="error_max_budget_usd", result="Invalid API key please run /login"), rc=1, diff="d", passed=True)
        assert r["error"] is None
        r, _ = repair_with(cj(is_error=True, subtype="error_during_execution", result="the authentication and billing modules look fine"), rc=1)
        assert r["error"] and "claude failed" in r["error"]
        # fatal text outside the result of an is_error run (here: rc!=0 but is_error false) is not fatal
        r, _ = repair_with(cj(is_error=False, result="Invalid API key"), rc=1)
        assert r["error"] and "claude failed" in r["error"]
        # real invoke_claude: on_start fires after the docker cp steps and before the claude command
        class Rec(FakeSession):
            def __init__(s, fail=False): super().__init__(); s.log, s.fail = [], fail
            def copy_to(s, src, dst):
                if s.fail: raise RuntimeError("cp failed")
                s.log.append("cp")
            def run_command(s, cmd, **k):
                s.log.append("claude" if isinstance(cmd, list) else "sh"); return super().run_command(cmd, **k)
        rs = Rec(); harness.invoke_claude(rs, "p", "m", lambda: rs.log.append("start"))
        assert rs.log.index("start") < rs.log.index("claude") and rs.log.index("cp") < rs.log.index("start")
        rs = Rec(fail=True); started = []
        try:
            harness.invoke_claude(rs, "p", "m", lambda: started.append(1)); raise SystemExit("cp should fail")
        except RuntimeError:
            assert not started
        # parsed $0 records $0; unparseable fatal output records the max
        for out, want in ((cj(is_error=True, result="Invalid API key", usage={"input_tokens": 5, "output_tokens": 0}), 0.0), ("Invalid API key", harness.MAX_REPAIR_USD)):
            sp0, sess = harness.spent(), FakeSession()
            o = SimpleNamespace(stdout=out, stderr="", returncode=1, timed_out=False)
            with mock.patch.object(harness, "DockerContainerSession", lambda *a, **k: sess), mock.patch.object(harness, "ensure_image", lambda i: i), \
                    mock.patch.object(harness, "setup_claude_in_container", lambda s: None), mock.patch.object(harness, "invoke_claude", lambda *a: o):
                try:
                    harness.run_repair(INST, [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]); raise SystemExit("not fatal")
                except harness.FatalRunError:
                    pass
            assert abs(harness.spent() - sp0 - want) < 1e-9, (out, harness.spent() - sp0)
        # `invoked` flips only when Claude is about to run: a failure before that costs $0, uncached, retried once
        f = [{"file": "z.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
        n = []
        def cp_fails(session, prompt, model, on_start=None):
            n.append(1); raise RuntimeError("docker cp failed")
        def after_start(session, prompt, model, on_start=None):
            on_start(); raise RuntimeError("connection lost")
        with mock.patch.object(harness, "DockerContainerSession", lambda *a, **k: FakeSession()), mock.patch.object(harness, "ensure_image", lambda i: i), \
                mock.patch.object(harness, "setup_claude_in_container", lambda s: None):
            sp0 = harness.spent()
            with mock.patch.object(harness, "invoke_claude", cp_fails):
                r = harness.repair_pr(INST, f)
                assert r["error"] and r["cost_usd"] == 0 and len(n) == 2 and harness.spent() == sp0  # retried once, no charge
                harness.repair_pr(INST, f); assert len(n) == 4  # not cached
            with mock.patch.object(harness, "invoke_claude", after_start):
                r = harness.repair_pr(INST, f)
            assert r["error"] and r["cost_usd"] == harness.MAX_REPAIR_USD


def test_evaluate_stops():
    insts = [harness.instances()[i] for i in evolve.sample("development", 10, 0)]
    calls = []
    def safe(inst, lib, cond):
        calls.append(inst["instance_id"])
        if len(calls) == 1:
            raise harness.BudgetExceeded("cap")
        time.sleep(0.05)
        return {"instance_id": inst["instance_id"], "passed": True}
    out = []
    with mock.patch.object(evolve, "_safe", safe), mock.patch.object(harness, "WORKERS", 1):
        try:
            evolve.evaluate(insts, [], "static", out); raise SystemExit("no stop")
        except harness.BudgetExceeded:
            pass
    assert len(calls) < 10 and len(out) == len(calls) - 1  # queued work cancelled; in-flight results kept
    calls.clear(); out.clear()
    def interrupt(inst, lib, cond):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        time.sleep(0.05)
        return {"instance_id": inst["instance_id"], "passed": True}
    with mock.patch.object(evolve, "_safe", interrupt), mock.patch.object(harness, "WORKERS", 1):
        try:
            evolve.evaluate(insts, [], "static", out); raise SystemExit("no interrupt")
        except KeyboardInterrupt:
            pass
    assert len(calls) < 10 and len(out) >= 1


def test_guards_and_summary():
    # I1: pilot refuses test-partition (and any non dev/selection) instances, before any key/ledger/Docker activity
    test_id = splits["instance_ids"]["test"][0]
    with tmp_state(), mock.patch.object(harness, "setup", side_effect=AssertionError("pilot got past the partition guard")):
        try:
            harness.pilot(test_id); raise SystemExit("pilot ran on test")
        except SystemExit as e:
            assert "refusing" in str(e)
        assert not harness.LEDGER.exists()
    # I2: setup aborts on an empty key
    with tempfile.TemporaryDirectory() as d, mock.patch.object(harness, "ENV_FILE", Path(d) / "none.env"), \
            mock.patch.dict("os.environ", {}, clear=False), mock.patch.object(harness, "LEDGER", Path(d) / "l.jsonl"):
        os.environ.pop("VOYAGER_API_KEY", None)
        try:
            harness.setup(); raise SystemExit("setup accepted empty key")
        except SystemExit as e:
            assert "VOYAGER_API_KEY" in str(e)
        assert not harness.LEDGER.exists()
        harness.get_key.cache_clear()
    # I7: both rates, errors listed; errored PR comments count as failed in the main number
    ids = evolve.sample("development", 2, 0)
    i0, i1 = [harness.instances()[i] for i in ids]
    n0, n1 = len(i0["reference_review_comments"]), len(i1["reference_review_comments"])
    res = [{"instance_id": ids[0], "repo": i0["repo"], "passed": True, "per_comment": [{"comment_index": k, "passed": True} for k in range(n0)]},
           {"instance_id": ids[1], "repo": i1["repo"], "passed": None, "per_comment": []}]
    sm = evolve.summarize(res)
    assert sm["n_errors"] == 1 and sm["errored_prs"] == [ids[1]] and sm["pass_rate_excl_errors"] == 1.0
    assert sm["pass_rate_errors_as_failed"] == n0 / (n0 + n1)
    rows = evolve.comment_rows(res)
    assert sum(r["error"] for r in rows) == n1 and not any(r["passed"] for r in rows if r["error"]) and len(rows) == n0 + n1
    # minor: results/ writes are scrubbed and atomic
    with tmp_state() as d:
        harness.write_json(d / "r" / "x.json", {"k": KEY}); harness.append_json(d / "r" / "y.jsonl", {"k": KEY})
        assert KEY not in (d / "r" / "x.json").read_text() + (d / "r" / "y.jsonl").read_text() and not list((d / "r").glob(".*tmp"))


def test_image_cleanup():
    calls = []
    def fake_run(cmd, **k):
        calls.append(cmd); return SimpleNamespace(returncode=0, stdout="", stderr="")
    a, b, c = "foo__bar-1@abc1234", "Baz__Qux-2@def5678", "never__used-3@0000000"
    with mock.patch.object(subprocess, "run", fake_run):
        assert harness.remove_image(a)
    assert calls == [["docker", "rmi", "reviewbench/foo__bar-1"], ["docker", "rmi", "ghcr.io/c-crab-benchmark/foo__bar-1:latest"]]
    flat = " ".join(" ".join(x) for x in calls)
    assert not any(w in flat for w in ("prune", "-f", "--force", "*")) 
    # only ids handed out by ensure_image are removed, once
    calls.clear()
    with mock.patch.object(harness, "docker_image_exists", lambda i: True):
        harness.ensure_image(a); harness.ensure_image(b)
    assert calls == []  # tracking alone runs nothing
    with mock.patch.object(subprocess, "run", fake_run):
        assert harness.cleanup_images() == 2
        assert harness.cleanup_images() == 0
    assert sorted(x[2] for x in calls) == sorted(["reviewbench/foo__bar-1", "ghcr.io/c-crab-benchmark/foo__bar-1:latest",
                                                  "reviewbench/baz__qux-2", "ghcr.io/c-crab-benchmark/baz__qux-2:latest"])
    assert not any("never" in x[2] for x in calls)
    # failures never raise; "no such image" is silent, other errors warn
    for res in (SimpleNamespace(returncode=1, stdout="", stderr="Error: No such image: x"),
                SimpleNamespace(returncode=1, stdout="", stderr="permission denied"), RuntimeError("docker missing")):
        def bad(cmd, **k):
            if isinstance(res, Exception): raise res
            return res
        with mock.patch.object(subprocess, "run", bad), warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert harness.remove_image(a) is False
            assert bool(w) == (not (hasattr(res, "stderr") and "No such image" in res.stderr))
    # cleanup runs after stops, in every entry point
    for exc in (harness.BudgetExceeded("cap"), harness.FatalRunError("auth")):
        for argv in (["evolve.py", "static", "--partition", "development", "--n", "2"],
                     ["evolve.py", "evolve", "--condition", "scoped", "--dev-n", "2", "--sel-n", "2", "--rounds", "1"]):
            n = []
            with mock.patch.object(sys, "argv", argv), mock.patch.object(harness, "setup", lambda: None), mock.patch.object(harness, "start_proxy", lambda: None), \
                    mock.patch.object(evolve, "sample", side_effect=exc), mock.patch.object(harness, "cleanup_images", lambda: n.append(1)):
                evolve.main()
            assert n == [1], (argv[1], exc)
    n = []
    with tmp_state(), mock.patch.object(harness, "setup", lambda: None), mock.patch.object(harness, "start_proxy", lambda: None), mock.patch.object(harness, "run_review", side_effect=harness.BudgetExceeded("cap")), \
            mock.patch.object(harness, "cleanup_images", lambda: n.append(1)):
        try:
            harness.pilot(splits["instance_ids"]["development"][0]); raise SystemExit("pilot swallowed stop")
        except harness.BudgetExceeded:
            pass
    assert n == [1]
    # run_round leaves image cleanup to the caller (the CLI cleans after each finished round, never mid-round)
    n = []
    with mock.patch.object(harness, "cleanup_images", lambda: n.append(1)):
        test_round()
    assert n == [], n  # test_round runs 3 rounds and never cleans up inside run_round


def test_proxy_hook():
    """Claude Code sends top-level system blocks plus a system-role message after the user turn; Qwen needs exactly one system string."""
    import proxy_hooks
    d = proxy_hooks.merge_system_prompts({"system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
                                          "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": [{"type": "text", "text": "C"}]}]})
    assert d["system"] == "A\n\nB\n\nC" and [m["role"] for m in d["messages"]] == ["user"]
    d = proxy_hooks.merge_system_prompts({"system": "only", "messages": [{"role": "user", "content": "x"}]})
    assert d["system"] == "only" and len(d["messages"]) == 1
    d = proxy_hooks.merge_system_prompts({"messages": [{"role": "user", "content": "x"}]})
    assert "system" not in d and len(d["messages"]) == 1


def test_cap_env():
    """SSE_CAP_USD parsing at import; check_budget is a no-op when the cap is disabled."""
    import importlib
    prev = os.environ.get("SSE_CAP_USD")
    try:
        for raw, want in (("5", 5.0), ("0.25", 0.25), ("", float("inf")), ("nan", float("inf")),
                          ("garbage", float("inf")), ("inf", float("inf"))):
            with mock.patch.dict(os.environ, {"SSE_CAP_USD": raw}, clear=False):
                importlib.reload(harness)
                assert harness.CAP_USD == want, (raw, harness.CAP_USD)
    finally:
        if prev is None:
            os.environ.pop("SSE_CAP_USD", None)
        else:
            os.environ["SSE_CAP_USD"] = prev
        importlib.reload(harness)  # restore the module constants used by the other tests
    with tmp_state():
        harness.record("review", "x", 9.0)
        for cap in (None, float("inf")):  # cap disabled: no accounting, never raises
            with mock.patch.object(harness, "CAP_USD", cap):
                harness.check_budget(1e12)
        with mock.patch.object(harness, "CAP_USD", 10.0):
            try:
                harness.check_budget(2.0); raise SystemExit("cap not enforced")
            except harness.BudgetExceeded:
                pass


def test_ledger_tokens():
    """The authoritative ledger records tokens, duration and model alongside the (nominal) cost."""
    with tmp_state():
        tokens = {"input_tokens": 11, "output_tokens": 7, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3}
        harness.record("repair", "owner__repo-1@abc1234", 0.0, tokens=tokens, duration_s=12.5, model="qwen36-27b")
        rec = json.loads(harness.LEDGER.read_text().splitlines()[0])
        assert rec["kind"] == "repair" and rec["instance"] == "owner__repo-1@abc1234" and rec["cost"] == 0.0
        assert rec["tokens"] == tokens and rec["duration_s"] == 12.5 and rec["model"] == "qwen36-27b" and "ts" in rec
        harness.record("review", "x", 0.03)
        rec = json.loads(harness.LEDGER.read_text().splitlines()[-1])
        assert rec["cost"] == 0.03 and not ({"tokens", "duration_s", "model"} & set(rec))
        assert abs(harness.spent() - 0.03) < 1e-9


def test_vendor_timeout_wrapper():
    """A vendor timeout with bytes on stderr (bytes + str -> TypeError) is turned into a timed_out result."""
    class S:
        container_id, _name = "cid", "sse-test"
        def start(self):
            pass
    s = S()
    s.run_command = harness.DockerContainerSession.run_command.__get__(s)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(["docker", "exec", "n"], 5, output=b"out", stderr=b"err")
    with mock.patch.object(subprocess, "run", timeout):
        r = s.run_command(["echo", "x"], timeout=5)
    assert r.timed_out and r.returncode == -1 and "timed out" in r.stderr
    # any other TypeError is not swallowed
    with mock.patch.object(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(TypeError("other boom"))):
        try:
            s.run_command(["echo"], timeout=5); raise SystemExit("swallowed an unrelated TypeError")
        except TypeError as e:
            assert "other boom" in str(e)


def test_rate_limit_retry():
    """A rate-limited repair is retried with bounded backoff; every attempt is recorded exactly once."""
    f = [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
    calls, sleeps = [], []
    def limited(inst, findings, model):
        calls.append(1)
        return {"per_comment": [{"comment_index": 0, "passed": None}], "cost_usd": 0.02,
                "error": "API Error: 429 rate_limit_error"}
    with tmp_state():
        with mock.patch.object(harness, "run_repair", limited), mock.patch.object(time, "sleep", lambda s: sleeps.append(s)):
            try:
                harness.repair_pr(INST, f); raise SystemExit("rate limit not fatal")
            except harness.FatalRunError as e:
                assert "after 2 retries" in str(e)
        ledger = [json.loads(l) for l in harness.LEDGER.read_text().splitlines()]
        assert len(calls) == 1 + len(harness.RATE_LIMIT_SLEEPS) == 3  # bounded retries
        assert sleeps == list(harness.RATE_LIMIT_SLEEPS)
        assert len(ledger) == 3 and [r["kind"] for r in ledger] == ["repair"] * 3
        assert abs(sum(r["cost"] for r in ledger) - 3 * 0.02) < 1e-9  # one record per attempt, no double charge
        # a later success returns normally: exactly one charge per attempt
        spent0 = harness.spent()
        calls.clear()
        seq = iter([{"per_comment": [{"comment_index": 0, "passed": None}], "cost_usd": 0.02, "error": "rate_limit_error"},
                    {"per_comment": [{"comment_index": 0, "passed": True}], "cost_usd": 0.5, "error": None}])
        f2 = [{"file": "b.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
        with mock.patch.object(harness, "run_repair", lambda *a, **k: calls.append(1) or next(seq)), \
                mock.patch.object(time, "sleep", lambda s: None):
            r = harness.repair_pr(INST, f2)
        assert len(calls) == 2 and r["error"] is None and r["cost_usd"] == 0.5
        assert abs(harness.spent() - spent0 - 0.52) < 1e-9  # both attempts charged once


def test_transient_403_retry():
    """A 403 unrecognized_model from the hosted endpoint is retried with backoff, not fatal on the first hit."""
    f = [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
    calls = []
    limit = {"per_comment": [{"comment_index": 0, "passed": None}], "cost_usd": 0.0,
             "error": "Failed to authenticate. API Error: 403 ASU error page success [claude-code:unrecognized_model]",
             "attempted": True, "transient": True}
    with tmp_state():
        with mock.patch.object(harness, "run_repair", lambda *a, **k: calls.append(1) or limit), \
                mock.patch.object(time, "sleep", lambda s: None):
            try:
                harness.repair_pr(INST, f); raise SystemExit("transient 403 was not retried")
            except harness.FatalRunError as e:
                assert "after 2 retries" in str(e)
        assert len(calls) == 1 + len(harness.RATE_LIMIT_SLEEPS) == 3
        # one transient then success returns the success
        calls.clear()
        seq = iter([limit, {"per_comment": [{"comment_index": 0, "passed": True}], "cost_usd": 0.0, "error": None}])
        with mock.patch.object(harness, "run_repair", lambda *a, **k: calls.append(1) or next(seq)), \
                mock.patch.object(time, "sleep", lambda s: None):
            r = harness.repair_pr(INST, f)
        assert len(calls) == 2 and r["error"] is None and r["per_comment"][0]["passed"] is True


def test_feedback_aggregation():
    """build_feedback aggregates per type, caps examples and findings, and carries rejected edits."""
    library = [{"id": "s1", "scope": "global", "text": "one", "order": 1},
               {"id": "s2", "scope": "global", "text": "two", "order": 2}]
    recs = []
    for i in range(15):
        ok = i % 2 == 0  # even PRs pass all of their comments; odd PRs fail all of them
        recs.append({"instance_id": f"owner/repo-{i}@sha", "repo": "owner/repo",
                     "pr_type": ("bug_fix", "feature", "other")[i % 3], "passed": ok,
                     "per_comment": [{"comment_index": k, "passed": ok} for k in range(2)],
                     "findings": [{"file": f"src/f{i}.py", "issue_header": f"header {i} src/f{i}.py",
                                   "issue_content": "body owner/repo-1@sha"}] * 5,
                     "skill_ids": ["s1", "s2"], "error": None})
    recs.append({"instance_id": "err/repo@x", "repo": "err/repo", "pr_type": "bug_fix", "passed": None,
                 "per_comment": [], "findings": [], "skill_ids": ["s1"], "error": "boom"})
    rejected = [{"op": "add", "scope": "global", "text": "bad owner/repo", "outcome": "rejected"} for _ in range(15)]
    rejected.append({"op": "add", "text": "accepted edit", "outcome": "accepted"})
    fb = evolve.build_feedback(recs, library, "global", rejected)
    assert fb["overall"] == {"prs": 16, "passed_prs": 8, "comments_passed": 16, "comments_total": 30, "errors": 1}
    assert fb["by_type"]["bug_fix"] == {"prs": 6, "passed_prs": 3, "comments_passed": 6, "comments_total": 10}
    assert fb["by_type"]["feature"] == {"prs": 5, "passed_prs": 2, "comments_passed": 4, "comments_total": 10}
    assert fb["by_type"]["other"] == {"prs": 5, "passed_prs": 3, "comments_passed": 6, "comments_total": 10}
    assert len(fb["examples"]) == 12  # cap 12, non-errored only
    # examples carry comments passed/total; failed PRs come first (0/2), then passing ones (2/2)
    assert [e["passed"] for e in fb["examples"]] == [0] * 7 + [2] * 5
    assert all(e["total"] == 2 for e in fb["examples"])
    assert all(len(e["findings"]) == 3 for e in fb["examples"])  # at most 3 findings each
    assert fb["skills"]["s1"] == {"retrieved": 16, "passed_prs": 8}
    assert fb["skills"]["s2"] == {"retrieved": 15, "passed_prs": 8}
    assert len(fb["rejected"]) == 12 and all(e["outcome"] == "rejected" for e in fb["rejected"])
    assert "accepted edit" not in json.dumps(fb)
    # sanitizer: the exact payload passes assert_clean, and a leaky payload is rejected
    assert "owner/repo" not in json.dumps(fb) and "owner/repo-1@sha" not in json.dumps(fb)
    evolve.assert_clean(fb, evolve._feedback_forbid(recs)[1])
    with mock.patch.object(evolve, "_clean", lambda t, f: str(t)):  # disable cleaning: the final assert must catch it
        try:
            evolve.build_feedback(recs, library, "global", rejected); raise SystemExit("forbidden string leaked")
        except ValueError:
            pass


def test_eligible_test_guard():
    """The test partition is refused unless unlocked, and unlocking requires a matching FROZEN.json code_sha."""
    try:
        evolve.eligible("test"); raise SystemExit("eligible allowed the locked test partition")
    except ValueError:
        pass
    split_text = (data.ROOT / "results/splits.json").read_text()
    test_ids = json.loads(split_text)["instance_ids"]["test"]
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "results").mkdir()
        (root / "results/splits.json").write_text(split_text)
        manifest = {"harness.py": "h", "evolve.py": "e", "reviewer.py": "r", "data.py": "d", "proxy_hooks.py": "p"}
        with mock.patch.object(evolve, "ROOT", root), mock.patch.object(evolve, "_code_sha", lambda: "abc123"), \
                mock.patch.object(evolve, "_code_manifest", lambda root=None: dict(manifest)):
            try:
                evolve.eligible("test", allow_test=True); raise SystemExit("missing FROZEN.json accepted")
            except RuntimeError as e:
                assert "FROZEN.json" in str(e)
            (root / "results/FROZEN.json").write_text(json.dumps({"code_sha": "abc123", "code_manifest": manifest}))
            by, ex = evolve.eligible("test", allow_test=True)
            assert set(ex) == {"no_image", "oversize"} and set(by) <= {"bug_fix", "feature", "other"}
            assert sum(len(v) for v in by.values()) + sum(len(v) for v in ex.values()) == len(test_ids)
            # a dirty working tree (manifest mismatch) is refused even when HEAD matches
            (root / "results/FROZEN.json").write_text(json.dumps({"code_sha": "abc123", "code_manifest": {"harness.py": "other"}}))
            try:
                evolve.eligible("test", allow_test=True); raise SystemExit("dirty-tree FROZEN.json accepted")
            except RuntimeError as e:
                assert "code_manifest" in str(e)
            (root / "results/FROZEN.json").write_text(json.dumps({"code_sha": "deadbeef", "code_manifest": manifest}))
            try:
                evolve.eligible("test", allow_test=True); raise SystemExit("mismatched FROZEN.json accepted")
            except RuntimeError as e:
                assert "does not match" in str(e)


def test_fresh_cache():
    """fresh=True salts the cache key (new evaluation, stored separately); normal calls still hit the cache."""
    calls = []
    def comp(**kw):
        calls.append(1); return fake_resp(GOOD)
    with tmp_state():
        with mock.patch.object(litellm, "completion", comp), mock.patch.object(litellm, "completion_cost", return_value=0.05):
            a = harness.run_review(INST, ["s"])
            b = harness.run_review(INST, ["s"])
            assert len(calls) == 1 and b["cached"] and b["cost_usd"] == 0 and b["findings"] == a["findings"]
            c = harness.run_review(INST, ["s"], fresh=True)
            assert len(calls) == 2 and not c.get("cached") and c["cost_usd"] == 0.05 and c["findings"] == a["findings"]
            d = harness.run_review(INST, ["s"])  # the unsalted key still exists
            assert len(calls) == 2 and d["cached"] and d["cost_usd"] == 0
        # the fresh result was stored under a different key, so the cache has two review entries
        assert len(list((harness.CACHE / "review").glob("*.json"))) == 2
        rcalls = []
        def repair(inst, findings, model):
            rcalls.append(1)
            return {"per_comment": [{"comment_index": 0, "passed": True}], "cost_usd": 0.0, "error": None}
        f = [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
        with mock.patch.object(harness, "run_repair", repair):
            r1 = harness.repair_pr(INST, f)
            r2 = harness.repair_pr(INST, f)
            assert len(rcalls) == 1 and r2["cached"]
            r3 = harness.repair_pr(INST, f, fresh=True)
            assert len(rcalls) == 2 and not r3.get("cached")
            r4 = harness.repair_pr(INST, f)
            assert len(rcalls) == 2 and r4["cached"] and r4["per_comment"] == r1["per_comment"]
        assert len(list((harness.CACHE / "repair").glob("*.json"))) == 2


def test_summarize_tokens():
    """summarize reports tokens and wall-clock duration across PRs, including errored ones."""
    ids = evolve.sample("development", 2, 0)
    i0, i1 = [harness.instances()[i] for i in ids]
    n0, n1 = len(i0["reference_review_comments"]), len(i1["reference_review_comments"])
    res = [{"instance_id": ids[0], "repo": i0["repo"], "passed": True,
            "per_comment": [{"comment_index": k, "passed": True} for k in range(n0)],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "duration_s": 2.4},
           {"instance_id": ids[1], "repo": i1["repo"], "passed": None, "per_comment": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1}, "duration_s": 1.1}]  # total derived as 4
    sm = evolve.summarize(res)
    assert sm["n_prs"] == 2 and sm["n_errors"] == 1 and sm["errored_prs"] == [ids[1]]
    assert (sm["tokens"], sm["prompt_tokens"], sm["completion_tokens"]) == (19, 13, 6)
    assert sm["duration_s"] == 3.5
    assert sm["n_comments"] == n0 + n1 and sm["pass_rate_errors_as_failed"] == n0 / (n0 + n1)


def test_round_logging():
    """run_round writes one JSONL record per round with a per-PR log entry for every development PR."""
    ids = evolve.sample("development", 2, 0)
    insts = [harness.instances()[i] for i in ids]
    dev = []
    for inst in insts:
        n = len(inst["reference_review_comments"])
        dev.append({"instance_id": inst["instance_id"], "repo": inst["repo"], "pr_type": data.pr_type(inst),
                    "skill_ids": [], "findings": [{"file": "a.py", "issue_header": "h", "issue_content": "c"}],
                    "per_comment": [{"comment_index": k, "passed": True} for k in range(n)],
                    "passed": True, "error": None, "cost_usd": 0.01,
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}, "duration_s": 1.5})
    edits = [{"op": "add", "scope": "global", "text": "guidance"}]
    with tmp_state() as d, mock.patch.object(evolve, "ROOT", d), mock.patch.object(evolve, "evaluate", lambda *a, **k: dev), \
            mock.patch.object(evolve, "propose_edits", lambda *a, **k: edits):
        lib, rec = evolve.run_round("scoped", [], insts, insts, gated=False, run_id="rid", round_no=1)
        path = d / "results/evolution/scoped.jsonl"
        assert path.exists()
        line = json.loads(path.read_text().splitlines()[0])
    assert rec["run_id"] == "rid" and rec["round"] == 1 and rec["condition"] == "scoped"
    assert rec["accept"] is True and rec["library_before"] == [] and len(rec["library_after"]) == 1
    assert rec["threshold"] == {"min_gain": 1, "max_regress": 1} and rec["dev_passed"] == {r["instance_id"]: True for r in dev}
    assert len(rec["prs"]) == len(dev)
    keys = {"instance_id", "repo", "pr_type", "skill_ids", "findings", "passed", "error", "cost_usd", "usage", "duration_s"}
    for logged, want in zip(rec["prs"], dev):
        assert set(logged) == keys
        assert logged["instance_id"] == want["instance_id"] and logged["usage"] == want["usage"]
        assert logged["duration_s"] == want["duration_s"] and logged["cost_usd"] == want["cost_usd"]
    assert line["prs"] == rec["prs"] and line["run_id"] == "rid" and line["dev"]["tokens"] == 24
    assert line["dev"]["duration_s"] == 3.0 and line["library_after"] == rec["library_after"]


def test_score_library_guard():
    """The frozen test score path requires allow_test, a matching code/library manifest, test ids and a salt, and writes once."""
    split_text = (data.ROOT / "results/splits.json").read_text()
    all_test = json.loads(split_text)["instance_ids"]["test"]
    known = harness.instances()
    test_ids = [i for i in all_test if i not in harness.NO_IMAGE
                and len(known[i]["commit_to_review"]["patch_to_review"]) <= reviewer.MAX_DIFF_CHARS]
    lib = [{"id": "s1", "scope": "global", "text": "general guidance", "order": 1}]
    manifest = {"harness.py": "h", "evolve.py": "e", "reviewer.py": "r", "data.py": "d", "proxy_hooks.py": "p"}
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "results").mkdir()
        (root / "results/splits.json").write_text(split_text)
        lib_path = root / "lib.json"
        lib_path.write_text(json.dumps(lib))
        try:
            evolve.score_library("test", "global", lib_path); raise SystemExit("score unlocked test")
        except ValueError:
            pass
        with mock.patch.object(evolve, "ROOT", root), mock.patch.object(evolve, "_code_sha", lambda: "abc123"), \
                mock.patch.object(evolve, "_code_manifest", lambda root=None: dict(manifest)):
            try:
                evolve.score_library("test", "global", lib_path, allow_test=True); raise SystemExit("no FROZEN accepted")
            except RuntimeError:
                pass
            (root / "results/FROZEN.json").write_text(json.dumps({"code_sha": "abc123", "code_manifest": manifest}))
            try:
                evolve.score_library("test", "global", lib_path, allow_test=True); raise SystemExit("no library hash accepted")
            except RuntimeError as e:
                assert "library hash" in str(e)
            (root / "results/FROZEN.json").write_text(json.dumps({"code_sha": "abc123", "code_manifest": manifest,
                                                                  "libraries": {"global": evolve.library_sha256(lib)},
                                                                  "test_ids": test_ids}))
            try:
                evolve.score_library("test", "global", lib_path, allow_test=True); raise SystemExit("no salt accepted")
            except SystemExit as e:
                assert "--salt" in str(e)
            # evaluate is offline here: score writes a stamped file exactly once
            def fake_eval(insts, library, condition, out, salt=None):
                out += [{"instance_id": i["instance_id"], "repo": i["repo"], "passed": True,
                         "per_comment": [{"comment_index": 0, "passed": True}], "cost_usd": 0.0, "usage": {}, "duration_s": 0.0}
                        for i in insts]
            with mock.patch.object(evolve, "evaluate", fake_eval), \
                    mock.patch.object(evolve, "comment_rows", lambda r: []):
                out = evolve.score_library("test", "global", lib_path, allow_test=True, salt="s1")
            assert out["library_sha256"] == evolve.library_sha256(lib) and out["salt"] == "s1"
            assert out["code_sha"] == "abc123" and (root / "results/test_global.json").exists()
            try:
                evolve.score_library("test", "global", lib_path, allow_test=True, salt="s1"); raise SystemExit("overwrote test score")
            except SystemExit as e:
                assert "already exists" in str(e)


def test_waf_blocked_repair():
    """A repair the ASU WAF keeps blocking, while the endpoint answers, is an infra error for that PR (not cached),
    never a whole-run stop. If the endpoint is down too, the run still stops."""
    f = [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
    blocked = {"per_comment": [{"comment_index": 0, "passed": None}], "cost_usd": 0.0, "attempted": True,
               "error": "claude failed (rc=1, subtype=success): Failed to authenticate. API Error: 403 ASU error page"}
    calls = []
    with tmp_state(), mock.patch.object(harness, "run_repair", lambda *a, **k: calls.append(1) or dict(blocked)), \
            mock.patch.object(time, "sleep", lambda s: None):
        with mock.patch.object(harness, "endpoint_ok", lambda: True):
            r = harness.repair_pr(INST, f, salt="s")
            assert r["content_blocked"] and "WAF" in r["error"] and harness.pr_passed(r) is None
            harness.repair_pr(INST, f, salt="s")
            assert len(calls) == 2 * (1 + len(harness.RATE_LIMIT_SLEEPS))  # not served from the cache
        with mock.patch.object(harness, "endpoint_ok", lambda: False):
            try:
                harness.repair_pr(INST, f, salt="s"); raise SystemExit("an endpoint outage did not stop the run")
            except harness.FatalRunError:
                pass


def test_evolve_loop_rounds():
    """A fresh run numbers rounds from 1, and empty-edit rounds count toward --stop-after-rejections (PROTOCOL §8)."""
    seen = []
    def fake_round(condition, lib, dev, sel, gated, run_id, rejected, mg, mr, salt, r):
        seen.append(r)
        return lib, {"accept": False, "edits": [], "gained": 0, "regressed": 0, "net": 0,
                     "dev": {"pass_rate_errors_as_failed": 0.0, "n_errors": 0}, "cost_usd": 0.0}
    argv = ["evolve.py", "evolve", "--condition", "scoped", "--dev-n", "2", "--sel-n", "2", "--rounds", "5",
            "--stop-after-rejections", "2", "--salt", "t"]
    ids = {p: evolve.sample(p, 2, 0) for p in ("development", "selection")}
    with tmp_state() as d, mock.patch.object(evolve, "ROOT", d), mock.patch.object(sys, "argv", argv), \
            mock.patch.object(evolve, "sample", lambda p, n, seed, allow_test=False: ids[p]), \
            mock.patch.object(evolve, "eligible", lambda p, allow_test=False: ({}, {})), \
            mock.patch.object(harness, "setup", lambda: None), mock.patch.object(harness, "start_proxy", lambda: None), \
            mock.patch.object(harness, "cleanup_images", lambda: None), mock.patch.object(evolve, "run_round", fake_round):
        evolve.main()
        log = [json.loads(l) for l in (d / "results/evolution/scoped.jsonl").read_text().splitlines()]
    assert seen == [1, 2], seen
    # a resumed run (new header) still sees edits the gate rejected before the resume
    with tempfile.TemporaryDirectory() as d, mock.patch.object(evolve, "ROOT", Path(d)):
        path = Path(d) / "results/evolution/scoped.jsonl"; path.parent.mkdir(parents=True)
        e1, e2 = {"op": "add", "scope": "global", "text": "one"}, {"op": "add", "scope": "global", "text": "two"}
        path.write_text("\n".join(json.dumps(r) for r in [
            {"run_id": "a", "dev_ids": []}, {"round": 1, "accept": False, "edits": [e1]},
            {"round": 2, "accept": False, "edits": []}, {"run_id": "b", "dev_ids": []},
            {"round": 2, "accept": False, "edits": [e2]}, {"round": 3, "aborted": "x"}]))
        assert evolve.rejected_from_log("scoped", 1) == [{**e1, "outcome": "rejected"}]
        assert [r["text"] for r in evolve.rejected_from_log("scoped", 2)] == ["one", "two"]
    assert log[-1]["stopped"] == "stop-after-rejections" and log[-1]["round"] == 2


if __name__ == "__main__":
    test_census(); test_splits(); test_reproducible()
    test_gate(); test_sanitizer(); test_retrieval(); test_ledger(); test_ledger_tokens(); test_reviewer(); test_review_waf_fallback(); test_cache()
    test_cap_env(); test_vendor_timeout_wrapper(); test_rate_limit_retry(); test_transient_403_retry(); test_feedback_aggregation()
    test_sample_scrub_optimizer(); test_stats(); test_round(); test_repair_failures(); test_evaluate_stops()
    test_guards_and_summary(); test_image_cleanup(); test_proxy_hook(); test_eligible_test_guard()
    test_fresh_cache(); test_summarize_tokens(); test_round_logging(); test_score_library_guard()
    test_waf_blocked_repair(); test_evolve_loop_rounds()
    print("all tests passed")
