"""Spend ledger, content-addressed cache, Docker repair run scored on Stage 4 tests, pilot CLI."""
import argparse, hashlib, json, math, os, re, subprocess, sys, tempfile, threading, time, uuid, warnings, zipfile
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
RAW = ROOT / "runs/raw"   # scrubbed raw claude output per repair run
HARNESS_VERSION = "1"     # bump when repair flow/scoring changes: invalidates repair cache
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
    """CLI entry: make the key visible to litellm in this (host) process only. Aborts before any paid/ledger activity if missing."""
    load_dotenv(ENV_FILE)
    get_key.cache_clear()
    if not get_key():
        raise SystemExit("ANTHROPIC_API_KEY is empty or missing: set it in the parent .env or the environment")


def scrub(text, key=None):
    key = get_key() if key is None else key
    return text.replace(key, "[REDACTED]") if key and isinstance(text, str) else text


def write_text(path, text):
    """Scrubbed, atomic (temp + rename). The only way we write under runs/ and results/."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(scrub(text))
    tmp.replace(path)


def write_json(path, obj):
    write_text(path, json.dumps(obj, indent=1, default=str))


def append_json(path, obj):
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(scrub(json.dumps(obj, default=str)) + "\n")


# ---------- spend ledger ----------
class StopRun(Exception):
    """Whole run must stop now (workers must not retry or continue)."""


class BudgetExceeded(StopRun):
    pass


class FatalRunError(StopRun):
    """Auth, credit-balance or rate-limit exhaustion: every further call would fail the same way."""


_lock = threading.Lock()
# ponytail: check-then-spend is not atomic; with WORKERS=2 overshoot is bounded by 2 reserves (CAP 22 vs 25 limit). Reserve under lock if workers grow.


def _spent():
    total = 0.0
    for l in (LEDGER.read_text().splitlines() if LEDGER.exists() else []):
        if not l.strip():
            continue
        try:
            total += json.loads(l)["cost"]
        except Exception:  # partial/corrupt line (crash mid-write): charge the max rather than crash or ignore it
            warnings.warn(f"corrupt ledger line, charging ${MAX_REPAIR_USD}: {l[:60]!r}")
            total += MAX_REPAIR_USD
    return total


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
    """fn() -> result dict. Hit: cost 0, no budget check. An errored result is stored iff it cost money (or was charged
    at the unknown-cost max), so re-evaluating never re-bills; unbilled infra errors are not stored and may retry."""
    p = CACHE / kind / f"{k}.json"
    if p.exists():
        old = json.loads(p.read_text())
        return {**old, "cached": True, "orig_cost_usd": old.get("cost_usd"), "cost_usd": 0.0}
    res = fn()
    if not res.get("error") or res.get("cost_usd", 0) > 0:
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


_used_images, _img_lock = set(), threading.Lock()


def image_names(iid):
    """The only two tags we ever remove for an instance."""
    return [get_docker_image_name(iid), f"ghcr.io/c-crab-benchmark/{image_slug(iid)}:latest"]


def remove_image(iid):
    """docker rmi of exactly the two tags above. No prune, no -f, no wildcards. Failures only warn. True if anything was removed."""
    removed = False
    for name in image_names(iid):
        try:
            r = subprocess.run(["docker", "rmi", name], capture_output=True, text=True)
            if r.returncode == 0:
                removed = True
            elif "no such image" not in (r.stderr or "").lower():
                warnings.warn(f"could not remove image {name}: {scrub(r.stderr or '')[:200]}")
        except Exception as e:
            warnings.warn(f"could not remove image {name}: {type(e).__name__}: {e}")
    return removed


def cleanup_images():
    """Remove every image ensure_image handed out in this run. Call between rounds/at the end, never mid-round."""
    with _img_lock:
        ids = sorted(_used_images)
        _used_images.clear()
    n = sum(remove_image(i) for i in ids)
    if ids:
        print(f"removed {n}/{len(ids)} benchmark images")
    return n


def ensure_image(iid):
    with _img_lock:
        _used_images.add(iid)
    img = get_docker_image_name(iid)
    if docker_image_exists(img):
        return img
    src = f"ghcr.io/c-crab-benchmark/{image_slug(iid)}:latest"
    subprocess.run(["docker", "pull", src], check=True, capture_output=True, text=True)
    subprocess.run(["docker", "tag", src, img], check=True, capture_output=True, text=True)
    return img


# CLI error forms only; prose that merely mentions "authentication" or "billing" must not stop a run
FATAL_RE = re.compile(r"invalid x-api-key|invalid api key|authentication_error|api error: ?40[13]|credit balance is too low|"
                      r"rate_limit_error|usage limit reached|please run /login", re.I)


def parse_claude(stdout):
    """Tolerant parse of `claude -p --output-format json`. Returns text, cost, ok, is_error, subtype.
    ok False (no usable cost) -> cost = MAX_REPAIR_USD, the unknown-cost charge."""
    out = {"text": "", "cost": MAX_REPAIR_USD, "ok": False, "is_error": False, "subtype": ""}
    try:
        o = json.loads(stdout)
        if isinstance(o, list):
            o = [x for x in o if isinstance(x, dict) and x.get("type") == "result"][-1]
        out.update(text=str(o.get("result", "")), is_error=bool(o.get("is_error")), subtype=str(o.get("subtype") or ""))
        cost = o["total_cost_usd"]
        assert isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0
        out.update(cost=float(cost), ok=True)
    except Exception:
        pass
    return out


def _put(session, text, dest):
    fd, p = tempfile.mkstemp()  # mode 600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        session.copy_to(p, dest)
    finally:
        os.unlink(p)


def invoke_claude(session, prompt, model, on_start=None):
    """Vendor invocation plus --output-format json and a spend cap. Key goes in via docker cp to a mode-600 file
    owned by agent; the command only names the file, sources it, and deletes it before claude starts."""
    _put(session, prompt, PROMPT_PATH)
    _put(session, f"export ANTHROPIC_API_KEY='{get_key()}'\n", KEY_PATH)
    session.run_command(f"chown agent:agent {PROMPT_PATH} {KEY_PATH} && chmod 600 {KEY_PATH}", timeout=10)
    if on_start:
        on_start()  # from here on spend is possible
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

    def mark():
        nonlocal invoked
        invoked = True
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
        r = invoke_claude(session, prompt, model, mark)  # sets `invoked` right before the Claude run; earlier failures cost $0
        write_json(RAW / f"{iid.replace('/', '__')}_{uuid.uuid4().hex[:8]}.json",
                   {"returncode": r.returncode, "timed_out": r.timed_out, "stdout": r.stdout, "stderr": r.stderr})
        p = parse_claude(r.stdout)
        failed = p["is_error"] or r.returncode != 0
        hay = " ".join([p["text"] if p["is_error"] else "", r.stderr[:500], "" if p["ok"] else r.stdout[:500]])
        if failed and not r.timed_out and p["subtype"] != "error_max_budget_usd" and FATAL_RE.search(hay):
            record("repair", iid, p["cost"])  # parsed cost (maybe $0); MAX only if the CLI ran and output was unparseable
            raise FatalRunError(f"Claude Code failed for {iid}, stopping the run: "
                                f"{scrub(' '.join([p['text'], p['subtype'], r.stderr[:200]]))[:300]}")
        # ponytail: a timeout is charged at the max and left as an error, so summaries count it failed (errors-as-failed) and the gate
        # excludes it; it is not scored on a partial diff. Score the diff instead if timeouts turn out common.
        res["cost_usd"] = MAX_REPAIR_USD if r.timed_out else p["cost"]  # unknown cost is charged at the max
        if r.timed_out or not p["ok"]:
            return done(error="claude timed out" if r.timed_out else f"claude output unparseable (rc={r.returncode})")
        diff = session.run_command("git diff", timeout=30).stdout
        res["agent_diff"] = scrub(diff)
        dirty = diff.strip() or session.run_command("git status --porcelain", timeout=15).stdout.strip()
        if failed and p["subtype"] != "error_max_budget_usd" and not dirty:
            # the agent did not really run: infra error, not a failed test
            return done(error=f"claude failed (rc={r.returncode}, subtype={p['subtype']}): {p['text'][:200]}")
        if not dirty:
            return done(passed=False)  # no changes = failed tests, not an infra error
        if lang == "python":
            session.run_command("pip install -e . --no-deps --quiet", timeout=120)
        for pc, (ci, i, e) in zip(res["per_comment"], tests):
            pc["passed"], _ = verify_with_test(session, e["test_code"], f"test_review_comment_{i}{EXT.get(lang, '.py')}", lang)
        res["duration_s"] = round(time.time() - t0, 1)
        return res
    except StopRun:
        raise
    except Exception as e:
        if invoked and not res["cost_usd"]:
            res["cost_usd"] = MAX_REPAIR_USD
        return done(error=f"{type(e).__name__}: {e}"[:500])
    finally:
        session.remove(force=True)


# ---------- budgeted, cached entry points ----------
def run_review(inst, skills, model=reviewer.MODEL):
    msgs = reviewer.build_messages(inst, skills)
    k = key("review", inst["instance_id"], json.dumps(msgs), model, reviewer.TEMPERATURE, reviewer.MAX_TOKENS, reviewer.RETRY_MAX_TOKENS)
    def go():
        check_budget(REVIEW_RESERVE_USD)
        return reviewer.review(inst, skills, model, on_cost=lambda c: record("review", inst["instance_id"], c))
    return cached("review", k, go)


def repair_pr(inst, findings, model=REPAIR_MODEL):
    if not findings:  # nothing to apply: no container, no cost
        return run_repair(inst, findings, model)
    prompt = build_tool_prompt(findings, inst["commit_to_review"]["patch_to_review"], inst["repo"])
    def go():
        check_budget(MAX_REPAIR_USD)
        r = run_repair(inst, findings, model)
        record("repair", inst["instance_id"], r["cost_usd"])
        if r["error"] and not r["cost_usd"]:  # unbilled infra error (e.g. docker pull/cp): retry once
            check_budget(MAX_REPAIR_USD)
            r = run_repair(inst, findings, model)
            record("repair", inst["instance_id"], r["cost_usd"])
        return r
    return cached("repair", key("repair", inst["instance_id"], HARNESS_VERSION, prompt, model, MAX_REPAIR_USD), go)


def pr_passed(rep):
    """True iff ALL Stage 4 tests pass; None if the repair errored (excluded from gating)."""
    if rep.get("error") or any(p["passed"] is None for p in rep["per_comment"]):
        return None
    return all(p["passed"] for p in rep["per_comment"])


def pilot(iid, skills=()):
    allowed = json.load(open(ROOT / "results/splits.json"))["instance_ids"]
    if iid not in allowed["development"] + allowed["selection"]:
        raise SystemExit(f"{iid} is not in the development or selection partition: refusing")
    setup()
    inst = instances()[iid]
    try:
        rv = run_review(inst, list(skills))
        rep = repair_pr(inst, rv["findings"])
    finally:
        cleanup_images()
    print(f"review cost ${rv['cost_usd']:.4f} findings {len(rv['findings'])} error {rv.get('error')}")
    print(f"repair cost ${rep['cost_usd']:.4f} duration {rep['duration_s']}s claude {rep['claude_version']} error {rep['error']}")
    print("per-test:", rep["per_comment"], "| PR passed:", pr_passed(rep))
    print("diff lines:", len(rep["agent_diff"].splitlines()), "| total spent $%.4f" % spent())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pilot").add_argument("--instance", required=True)
    a = ap.parse_args()
    try:
        pilot(a.instance)
    except StopRun as e:
        print("STOPPED:", e)
