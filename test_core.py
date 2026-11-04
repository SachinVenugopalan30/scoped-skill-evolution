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
import os, socket, subprocess, tempfile, threading, time, warnings
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
    assert g({"a": False}, {"a": True}) == {"accept": True, "gained": 1, "regressed": 0, "excluded": []}
    assert not g({"a": False, "b": True}, {"a": False, "b": True})["accept"]
    assert g({"a": False, "b": False, "c": True, "d": False}, {"a": True, "b": True, "c": False, "d": False})["accept"]  # 2/1
    assert not g({"a": False, "b": False, "c": True, "d": True}, {"a": True, "b": True, "c": False, "d": False})["accept"]  # 2/2
    r = g({"a": False, "b": True}, {"a": True, "b": None})
    assert r["regressed"] == 0 and r["excluded"] == ["b"] and r["accept"]
    assert g({"a": None}, {"a": True})["gained"] == 0
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
    assert ids("scoped", "bug_fix") == ["s2", "s1", "s4"]  # type-scoped first, then global; excludes other types
    assert ids("scoped", "other") == ["s1", "s4"]
    assert ids("static", "bug_fix") == []
    assert ids("global", "bug_fix") == ["s1", "s4"]  # global condition never sees non-global
    tok = lambda t: litellm.token_counter(model=reviewer.MODEL, text=t)
    big = {"id": "g1", "scope": "global", "text": "word " * 60}
    small = {"id": "g2", "scope": "global", "text": "tiny"}
    typed = {"id": "t1", "scope": "bug_fix", "text": "typed skill"}
    budget = tok("\n" + typed["text"] + "\n" + small["text"]) + 2
    assert tok(big["text"]) > budget
    got = evolve.retrieve([big, small, typed], "bug_fix", "scoped", budget)
    assert [s["id"] for s in got] == ["t1", "g2"]  # skip-and-continue: oversize skill skipped, later small one kept
    assert tok("\n".join(s["text"] for s in got)) <= budget
    assert ids("global", "bug_fix", budget, [big, small]) == ["g2"]


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
        assert harness.spent() - 0.6 == harness.MAX_REPAIR_USD  # unknown cost is the max, not 0
        harness.record("repair", "x", float("nan"))
        assert harness.spent() - 0.6 == 2 * harness.MAX_REPAIR_USD
        # unparseable / timed-out claude output is charged at the max
        g = harness.parse_claude("garbage")
        assert g["cost"] == harness.MAX_REPAIR_USD and not g["ok"]
        p = harness.parse_claude('{"type":"result","result":"hi","total_cost_usd":0.2,"is_error":true,"subtype":"x"}')
        assert (p["text"], p["cost"], p["ok"], p["is_error"], p["subtype"]) == ("hi", 0.2, True, True, "x")
        assert not harness.parse_claude('{"result":"no cost"}')["ok"]
        # corrupt trailing ledger line is counted conservatively, not a crash
        before = harness.spent()
        with harness.LEDGER.open("a") as f:
            f.write('{"kind": "repair", "inst')
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert harness.spent() == before + harness.MAX_REPAIR_USD and w
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
    assert len(calls) == 2 and r["findings"] == [] and r["error"] and abs(r["cost_usd"] - 2 * (1000 * 3 + 100 * 15) / 1e6) < 1e-12
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
    assert reviewer.parse_findings('[{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c", "w": [1]},{"fi') is None
    seq = iter([fake_resp("nope"), fake_resp(GOOD)])
    with mock.patch.object(litellm, "completion", lambda **kw: next(seq)), mock.patch.object(litellm, "completion_cost", return_value=0.01):
        r = reviewer.review(INST, [])
    assert len(r["findings"]) == 1 and "error" not in r and r["cost_usd"] == 0.02


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
            calls.append(1); return {"per_comment": ok(), "cost_usd": harness.MAX_REPAIR_USD, "error": "claude timed out"}
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
            with mock.patch.object(harness, "HARNESS_VERSION", "2"):
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
    ed = evolve.parse_edits(raw, lib, "scoped")
    assert len(ed) == 2 and ed[0]["scope"] == "bug_fix" and len(ed[0]["text"].split()) == 60 and ed[1] == {"op": "delete", "id": "s1"}
    assert all(e["scope"] == "global" for e in evolve.parse_edits(raw, lib, "global") if e["op"] == "add")
    assert evolve.apply_edits(lib, ed) == [{"id": "s2", "scope": "bug_fix", "text": ed[0]["text"]}]
    # propose_edits: asserts clean right before the call, records cost, refuses forbidden content
    with tmp_state(), mock.patch.object(litellm, "completion", lambda **kw: fake_resp(raw)), mock.patch.object(litellm, "completion_cost", return_value=0.02):
        out = evolve.propose_edits(lib, [{"pr_type": "other", "findings": [], "passed": 0, "total": 1}], "global", ["LEAKME"])
        assert len(out) == 2 and harness.spent() == 0.02
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


def test_repair_failures():
    cj = lambda **kw: json.dumps({"type": "result", "total_cost_usd": 0.4, **kw})
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
        # auth / credit / rate-limit: fatal, stops the run, no phantom charge, no secret in the message
        for msg in ("Invalid API key · Please run /login", "Credit balance is too low", "rate limit reached", "usage limit exceeded"):
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
                assert KEY not in str(e) and harness.spent() == spent0 and sess.removed
        assert KEY not in "".join(p.read_text() for p in harness.RAW.glob("*.json"))


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
        os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            harness.setup(); raise SystemExit("setup accepted empty key")
        except SystemExit as e:
            assert "ANTHROPIC_API_KEY" in str(e)
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


if __name__ == "__main__":
    test_census(); test_splits(); test_reproducible()
    test_gate(); test_sanitizer(); test_retrieval(); test_ledger(); test_reviewer(); test_cache()
    test_sample_scrub_optimizer(); test_stats(); test_round(); test_repair_failures(); test_evaluate_stops(); test_guards_and_summary()
    print("all tests passed")
