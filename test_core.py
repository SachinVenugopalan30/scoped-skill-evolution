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
import socket, subprocess, tempfile, threading
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
            mock.patch.object(harness, "CACHE", Path(d) / "cache"), mock.patch.object(harness, "get_key", lambda: KEY):
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
    ids = lambda c, t, b=400: [s["id"] for s in evolve.retrieve(lib, t, c, b)]
    assert ids("scoped", "bug_fix") == ["s1", "s2", "s4"] and ids("scoped", "other") == ["s1", "s4"]
    assert ids("static", "bug_fix") == []
    assert ids("global", "bug_fix") == ["s1", "s4"]  # global condition never sees non-global
    tok = lambda t: litellm.token_counter(model=reviewer.MODEL, text=t)
    one = tok("check inputs")
    got = evolve.retrieve(lib, "bug_fix", "scoped", one + 3)
    assert [s["id"] for s in got] == ["s1"] and tok("\n".join(s["text"] for s in got)) <= one + 3


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
        assert harness.parse_claude("garbage") == ("", harness.MAX_REPAIR_USD, False)
        assert harness.parse_claude('{"type":"result","result":"hi","total_cost_usd":0.2}') == ("hi", 0.2, True)
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
        # errors are not cached
        with mock.patch.object(litellm, "completion", lambda **kw: fake_resp("junk")), mock.patch.object(litellm, "completion_cost", return_value=0.01):
            e = harness.run_review(INST, ["err"])
        assert e["error"] and not list((harness.CACHE / "review").glob(f"{harness.key('review', INST['instance_id'], INST['commit_to_review']['patch_to_review'], ['err'], reviewer.MODEL)}.json"))
        assert harness.spent() > s1  # but its cost was still recorded
        # empty findings: no repair run, no container, zero cost, all failed
        rep = harness.repair_pr(INST, [])
        assert rep["cost_usd"] == 0 and rep["error"] is None and all(p["passed"] is False for p in rep["per_comment"])
        # repair cache: errored not stored, ok stored and then hit at $0
        calls = []
        def fake_repair(inst, findings, model):
            calls.append(1)
            return {"per_comment": [{"comment_index": 0, "passed": True}], "cost_usd": 0.5, "error": "boom" if len(calls) == 1 else None}
        with mock.patch.object(harness, "run_repair", fake_repair):
            f = [{"file": "a.py", "line": 1, "issue_header": "h", "issue_content": "c"}]
            harness.repair_pr(INST, f); harness.repair_pr(INST, f)
            h = harness.repair_pr(INST, f)
        assert len(calls) == 2 and h["cached"] and h["cost_usd"] == 0
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
    assert a == evolve.sample("selection", 12, 3) and len(a) == 12 and a != evolve.sample("selection", 12, 4)
    assert not set(a) & harness.NO_IMAGE
    inst = harness.instances()
    dev_all = splits["instance_ids"]["development"]
    big = evolve.sample("development", len(dev_all), 0)
    assert len(big) == len(dev_all) - len(set(dev_all) & harness.NO_IMAGE) and not set(big) & harness.NO_IMAGE
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


if __name__ == "__main__":
    test_census(); test_splits(); test_reproducible()
    test_gate(); test_sanitizer(); test_retrieval(); test_ledger(); test_reviewer(); test_cache()
    test_sample_scrub_optimizer(); test_stats(); test_round()
    print("all tests passed")
