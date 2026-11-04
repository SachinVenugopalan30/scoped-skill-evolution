"""Spend ledger, content-addressed cache, Docker repair run scored on Stage 4 tests, pilot CLI."""
import argparse, hashlib, json, math, os, subprocess, sys, tempfile, threading, time, uuid, zipfile
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

import data, reviewer

ROOT = data.ROOT
VENDOR = ROOT / "vendor/c-crab"
sys.path.insert(0, str(VENDOR))  # vendor is import-only, never modified
from execution.container_runtime import DockerContainerSession, docker_image_exists, get_docker_image_name  # noqa: E402
from pipeline.agent_resolver import CLAUDE_TIMEOUT, build_tool_prompt, setup_claude_in_container, verify_with_test  # noqa: E402

os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
ENV_FILE = ROOT.parent / ".env"
TESTGEN_ZIP = VENDOR / "raw_results_compressed/testgen_combined.zip"
LEDGER = ROOT / "runs/ledger.jsonl"
CACHE = ROOT / "runs/cache"
CAP_USD = 22.0
MAX_REPAIR_USD = 1.5       # passed to claude --max-budget-usd; also the charge when real cost is unknown
REVIEW_RESERVE_USD = 0.3
REPAIR_MODEL = "claude-sonnet-4-6"
WORKERS = 2                # Docker has 8 GB
NO_IMAGE = {"python__cpython-104029@f78e70e"}
EXT = {"python": ".py", "javascript": ".test.js", "typescript": ".test.ts", "go": "_test.go"}
CLAUDE_BIN, PROMPT_PATH, KEY_PATH = "/home/agent/.local/bin/claude", "/home/agent/prompt.txt", "/home/agent/.anthropic_env"


# ---------- secrets ----------
@lru_cache
def get_key():
    return os.environ.get("ANTHROPIC_API_KEY") or dotenv_values(ENV_FILE).get("ANTHROPIC_API_KEY") or ""


def setup():
    """CLI entry: make the key visible to litellm in this (host) process only."""
    load_dotenv(ENV_FILE)


def scrub(text, key=None):
    key = get_key() if key is None else key
    return text.replace(key, "[REDACTED]") if key and isinstance(text, str) else text


def write_json(path, obj):
    """Only way we write under runs/: key is scrubbed from the serialized text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(json.dumps(obj, indent=1, default=str)))


# ---------- spend ledger ----------
class BudgetExceeded(Exception):
    pass


_lock = threading.Lock()
# ponytail: check-then-spend is not atomic; with WORKERS=2 overshoot is bounded by 2 reserves (CAP 22 vs 25 limit). Reserve under lock if workers grow.


def _spent():
    return sum(json.loads(l)["cost"] for l in LEDGER.read_text().splitlines() if l.strip()) if LEDGER.exists() else 0.0


def spent():
    with _lock:
        return _spent()


def check_budget(reserve):
    s = spent()
    if s + reserve > CAP_USD:
        raise BudgetExceeded(f"spent ${s:.2f} + reserve ${reserve:.2f} > cap ${CAP_USD:.2f}")


def record(kind, instance, cost, unknown=MAX_REPAIR_USD):
    if cost is None or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        cost = unknown  # never record unknown spend as zero
    with _lock:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps({"kind": kind, "instance": instance, "cost": cost, "ts": time.time()}) + "\n")


# ---------- cache ----------
def key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def cached(kind, k, fn):
    """fn() -> result dict. Hit: cost 0, no budget check. Errored results are not stored."""
    p = CACHE / kind / f"{k}.json"
    if p.exists():
        old = json.loads(p.read_text())
        return {**old, "cached": True, "orig_cost_usd": old.get("cost_usd"), "cost_usd": 0.0}
    res = fn()
    if not res.get("error"):
        write_json(p, res)
    return res


# ---------- data ----------
@lru_cache
def instances():
    return {r["instance_id"]: r for r in data.load(data.STAGES[-1])}


def stage4_tests(inst):
    """[(comment_index into reference_review_comments, testgen results[] index, entry)], matched by exact comment_text."""
    with zipfile.ZipFile(TESTGEN_ZIP) as z:
        d = json.loads(z.read(f"testgen_combined/{inst['instance_id'].replace('/', '__')}/result.json"))
    out = []
    for ci, c in enumerate(inst["reference_review_comments"]):
        m = [(i, e) for i, e in enumerate(d["results"]) if e.get("success") and e["comment_text"] == c["text"]]
        if len(m) != 1:
            raise ValueError(f"{inst['instance_id']} comment {ci}: {len(m)} matching tests")
        out.append((ci, *m[0]))
    return out


# ---------- docker ----------
def image_slug(iid):
    return iid.split("@")[0].lower()


def ensure_image(iid):
    img = get_docker_image_name(iid)
    if docker_image_exists(img):
        return img
    src = f"ghcr.io/c-crab-benchmark/{image_slug(iid)}:latest"
    subprocess.run(["docker", "pull", src], check=True, capture_output=True, text=True)
    subprocess.run(["docker", "tag", src, img], check=True, capture_output=True, text=True)
    return img


def parse_claude(stdout):
    """(result_text, cost, ok). Unparseable output or missing cost -> cost = MAX_REPAIR_USD, ok False."""
    try:
        o = json.loads(stdout)
        if isinstance(o, list):
            o = [x for x in o if isinstance(x, dict) and x.get("type") == "result"][-1]
        cost = o["total_cost_usd"]
        assert isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0
        return str(o.get("result", "")), float(cost), True
    except Exception:
        return "", MAX_REPAIR_USD, False


def _put(session, text, dest):
    fd, p = tempfile.mkstemp()  # mode 600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        session.copy_to(p, dest)
    finally:
        os.unlink(p)


def invoke_claude(session, prompt, model):
    """Vendor invocation plus --output-format json and a spend cap. Key goes in via docker cp to a mode-600 file
    owned by agent; the command only names the file, sources it, and deletes it before claude starts."""
    _put(session, prompt, PROMPT_PATH)
    _put(session, f"export ANTHROPIC_API_KEY='{get_key()}'\n", KEY_PATH)
    session.run_command(f"chown agent:agent {PROMPT_PATH} {KEY_PATH} && chmod 600 {KEY_PATH}", timeout=10)
    r = session.run_command(
        ["su", "-", "agent", "-s", "/bin/bash", "-c",
         f"cd /workspace && . {KEY_PATH} && rm -f {KEY_PATH} && cat {PROMPT_PATH} | {CLAUDE_BIN} -p "
         f"--dangerously-skip-permissions --model {model} --output-format json --max-budget-usd {MAX_REPAIR_USD}"],
        timeout=CLAUDE_TIMEOUT)
    session.run_command(f"rm -f {KEY_PATH}", timeout=10)
    return r


def run_repair(inst, findings, model=REPAIR_MODEL):
    """One container, one Claude Code invocation, score every Stage 4 test of the PR."""
    t0 = time.time()
    iid, tests = inst["instance_id"], stage4_tests(inst)
    res = {"instance_id": iid, "repo": inst["repo"], "per_comment": [{"comment_index": ci, "passed": None} for ci, _, _ in tests],
           "agent_diff": "", "cost_usd": 0.0, "error": None, "duration_s": 0.0, "claude_version": None}

    def done(error=None, passed=None):
        res["error"] = scrub(error) if error else None
        for pc in res["per_comment"]:
            pc["passed"] = passed
        res["duration_s"] = round(time.time() - t0, 1)
        return res

    if not findings:
        return done(passed=False)
    if iid in NO_IMAGE:
        return done(error="no docker image for this instance")
    session = DockerContainerSession(get_docker_image_name(iid), name=f"sse-{uuid.uuid4().hex[:12]}")
    invoked = False
    try:
        ensure_image(iid)
        session.start()
        setup_claude_in_container(session)
        ver = session.run_command(["su", "-", "agent", "-s", "/bin/bash", "-c", f"{CLAUDE_BIN} --version"], timeout=60)
        res["claude_version"] = ver.stdout.strip() or None
        lang = tests[0][2]["language"]
        reset = session.run_command(
            f"git checkout --force {inst['commit_to_review']['head_commit']} && git clean -fd --quiet", timeout=120)
        if reset.returncode != 0:
            return done(error=f"git checkout failed: {reset.stderr[:300]}")
        if lang == "python":
            session.run_command("pip install -e . --no-deps --quiet", timeout=120)
        prompt = build_tool_prompt(findings, inst["commit_to_review"]["patch_to_review"], inst["repo"])  # vendor prompt, unchanged
        invoked = True
        r = invoke_claude(session, prompt, model)
        _, cost, ok = parse_claude(r.stdout)
        res["cost_usd"] = MAX_REPAIR_USD if r.timed_out else cost  # unknown cost is charged at the max
        if r.timed_out or not ok:
            return done(error="claude timed out" if r.timed_out else f"claude output unparseable (rc={r.returncode})")
        diff = session.run_command("git diff", timeout=30).stdout
        res["agent_diff"] = scrub(diff)
        if not diff.strip() and not session.run_command("git status --porcelain", timeout=15).stdout.strip():
            return done(passed=False)  # no changes = failed tests, not an infra error
        if lang == "python":
            session.run_command("pip install -e . --no-deps --quiet", timeout=120)
        for pc, (ci, i, e) in zip(res["per_comment"], tests):
            pc["passed"], _ = verify_with_test(session, e["test_code"], f"test_review_comment_{i}{EXT.get(lang, '.py')}", lang)
        res["duration_s"] = round(time.time() - t0, 1)
        return res
    except Exception as e:
        if invoked and not res["cost_usd"]:
            res["cost_usd"] = MAX_REPAIR_USD
        return done(error=f"{type(e).__name__}: {e}"[:500])
    finally:
        session.remove(force=True)


# ---------- budgeted, cached entry points ----------
def run_review(inst, skills, model=reviewer.MODEL):
    k = key("review", inst["instance_id"], inst["commit_to_review"]["patch_to_review"], list(skills), model)
    def go():
        check_budget(REVIEW_RESERVE_USD)
        r = reviewer.review(inst, skills, model)
        record("review", inst["instance_id"], r["cost_usd"])
        return r
    return cached("review", k, go)


def canonical(findings):
    return json.dumps(findings, sort_keys=True)


def repair_pr(inst, findings, model=REPAIR_MODEL):
    if not findings:  # nothing to apply: no container, no cost
        return run_repair(inst, findings, model)
    def go():
        check_budget(MAX_REPAIR_USD)
        r = run_repair(inst, findings, model)
        record("repair", inst["instance_id"], r["cost_usd"])
        return r
    return cached("repair", key("repair", inst["instance_id"], canonical(findings), model), go)


def pr_passed(rep):
    """True iff ALL Stage 4 tests pass; None if the repair errored (excluded from gating)."""
    if rep.get("error") or any(p["passed"] is None for p in rep["per_comment"]):
        return None
    return all(p["passed"] for p in rep["per_comment"])


def pilot(iid, skills=()):
    setup()
    inst = instances()[iid]
    rv = run_review(inst, list(skills))
    rep = repair_pr(inst, rv["findings"])
    print(f"review cost ${rv['cost_usd']:.4f} findings {len(rv['findings'])} error {rv.get('error')}")
    print(f"repair cost ${rep['cost_usd']:.4f} duration {rep['duration_s']}s claude {rep['claude_version']} error {rep['error']}")
    print("per-test:", rep["per_comment"], "| PR passed:", pr_passed(rep))
    print("diff lines:", len(rep["agent_diff"].splitlines()), "| total spent $%.4f" % spent())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pilot").add_argument("--instance", required=True)
    a = ap.parse_args()
    pilot(a.instance)
