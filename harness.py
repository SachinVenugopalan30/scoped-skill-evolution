"""Spend ledger, content-addressed cache, Docker repair run scored on Stage 4 tests, pilot CLI."""
import argparse, atexit, hashlib, json, math, os, re, secrets, shutil, subprocess, sys, tempfile, threading, time, urllib.request, uuid, warnings, zipfile
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values, load_dotenv

import data, reviewer

ROOT = data.ROOT
VENDOR = ROOT / "vendor/c-crab"
sys.path.insert(0, str(VENDOR))  # vendor is import-only, never modified
from execution.container_runtime import DockerContainerSession, docker_image_exists, get_docker_image_name  # noqa: E402
from pipeline.agent_resolver import CLAUDE_TIMEOUT, build_tool_prompt, setup_claude_in_container, verify_with_test  # noqa: E402


def _wrap_vendor_run_command():
    """The vendor timeout handler does `exc.stderr + str` and dies with "can't concat str to bytes" when the process wrote to
    stderr. Treat that as the timeout it is, instead of crashing the whole run; vendor/ stays untouched."""
    if getattr(DockerContainerSession.run_command, "_sse_timeout_guard", False):
        return
    orig = DockerContainerSession.run_command

    def run_command(self, *args, **kwargs):
        try:
            return orig(self, *args, **kwargs)
        except TypeError as e:
            if "can't concat str to bytes" not in str(e):
                raise
            return SimpleNamespace(returncode=-1, stdout="", stderr="\nCommand timed out (vendor timeout handler crash)",
                                   timed_out=True)

    run_command._sse_timeout_guard = True
    DockerContainerSession.run_command = run_command


_wrap_vendor_run_command()

os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
ENV_FILE = ROOT / ".env"
TESTGEN_ZIP = VENDOR / "raw_results_compressed/testgen_combined.zip"
LEDGER = ROOT / "runs/ledger.jsonl"
CACHE = ROOT / "runs/cache"
RAW = ROOT / "runs/raw"   # scrubbed raw claude output per repair run
HARNESS_VERSION = "5"     # bump when repair flow/scoring changes: invalidates repair cache
try:
    CAP_USD = float(os.environ.get("SSE_CAP_USD", "inf"))  # uncapped by default: ASU hosting is free
    if math.isnan(CAP_USD):
        CAP_USD = float("inf")
except ValueError:  # empty/garbage env value: uncapped rather than crashing at import
    CAP_USD = float("inf")
# Free model: an unknown-cost repair is $0. Paid model: charge the max so an unparseable/timeout run is never undercounted.
MAX_REPAIR_USD = 0.0 if reviewer.PRICE_IN == 0 and reviewer.PRICE_OUT == 0 else 0.25
REVIEW_RESERVE_USD = 0.3
REPAIR_MODEL = "qwen36-27b"  # alias served by the local proxy (below)
WORKERS = int(os.environ.get("SSE_WORKERS", "2"))  # Docker has 8 GB; 3 is safe on a 24 GB host, 4 is not
REPAIR_TIMEOUT = 1200      # seconds per Claude run; overrides the vendor's CLAUDE_TIMEOUT (600)
PIP_TIMEOUT = 300          # seconds for `pip install -e .`; emulated amd64 builds exceed 120 s on large repos
KEEP_IMAGES = os.environ.get("SSE_KEEP_IMAGES") == "1"  # debug: leave benchmark images in place
MIN_FREE_GB = float(os.environ.get("SSE_MIN_FREE_GB", "30"))  # refuse to pull/start below this much host disk
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
NO_IMAGE = {"python__cpython-104029@f78e70e"}
EXT = {"python": ".py", "javascript": ".test.js", "typescript": ".test.ts", "go": "_test.go"}
CLAUDE_BIN, PROMPT_PATH, KEY_PATH = "/home/agent/.local/bin/claude", "/home/agent/prompt.txt", "/home/agent/.anthropic_env"
PROXY_PORT, PROXY_TOKEN, _proxy = 4000, "unset", None  # token is random per run; only the local proxy accepts it
PROXY_URL_IN_CONTAINER = f"http://host.docker.internal:{PROXY_PORT}"  # Docker Desktop; on Linux add --add-host


# ---------- secrets ----------
@lru_cache
def get_key():
    return os.environ.get("VOYAGER_API_KEY") or dotenv_values(ENV_FILE).get("VOYAGER_API_KEY") or ""


def setup():
    """CLI entry: make the key visible to litellm in this (host) process only. Aborts before any paid/ledger activity if missing."""
    load_dotenv(ENV_FILE)
    get_key.cache_clear()
    if not get_key():
        raise SystemExit("VOYAGER_API_KEY is empty or missing: set it in .env (project root) or the environment")
    check_disk()


def free_gb(path=None):
    return shutil.disk_usage(path or ROOT).free / 1e9


def check_disk(min_free_gb=None):
    """A full disk crashes Docker Desktop and silently turns into failed reviews. Refuse to start, and say how to fix it."""
    need = MIN_FREE_GB if min_free_gb is None else min_free_gb
    free = free_gb()
    if free < need:
        raise SystemExit(f"only {free:.1f} GB free; need {need:.0f} GB before a run. "
                         f"Remove leftover benchmark images: docker images | grep -E 'c-crab-benchmark|reviewbench'")
    return free


def _stop_proxy():
    if _proxy and _proxy.poll() is None:
        _proxy.terminate()
        try:
            _proxy.wait(15)
        except subprocess.TimeoutExpired:
            _proxy.kill()


def start_proxy():
    """Local LiteLLM proxy on loopback: Claude Code speaks the Anthropic API, ASU Voyager speaks OpenAI's. The ASU key stays on the
    host; containers only get PROXY_TOKEN, which this proxy alone accepts. Stopped at exit. Call after setup()."""
    global _proxy, PROXY_TOKEN
    if _proxy:
        return
    cfg = ROOT / "runs/proxy.yaml"
    write_text(cfg, "model_list:\n  - model_name: " + REPAIR_MODEL + "\n    litellm_params:\n      model: hosted_vllm/qwen36-27b"  # not openai/: its Anthropic streaming drops tool-call arguments (empty tool inputs)
               + ""
               + "\n      api_base: " + reviewer.API_BASE + "\n      api_key: os.environ/VOYAGER_API_KEY\n      extra_body:\n"
               "        chat_template_kwargs: {enable_thinking: false}\n"
               "litellm_settings:\n  callbacks: proxy_hooks.merge_system\n")  # proxy_hooks.py: one system message only
    PROXY_TOKEN = "sk-" + secrets.token_hex(12)
    env = {**os.environ, "VOYAGER_API_KEY": get_key(), "LITELLM_MASTER_KEY": PROXY_TOKEN, "PYTHONPATH": str(ROOT)}
    exe = Path(sys.executable).parent / "litellm"
    _proxy = subprocess.Popen([str(exe), "--config", str(cfg), "--host", "127.0.0.1", "--port", str(PROXY_PORT)], env=env,
                              stdout=open(ROOT / "runs/proxy.log", "w"), stderr=subprocess.STDOUT)
    atexit.register(_stop_proxy)
    for _ in range(90):
        if _proxy.poll() is not None:
            raise SystemExit(f"proxy exited early (port {PROXY_PORT} busy?); see runs/proxy.log")
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/health/liveliness", timeout=2).status == 200:
                return
        except Exception:
            time.sleep(1)
    raise SystemExit("proxy did not start in 90s; see runs/proxy.log")


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
    if CAP_USD is None or math.isinf(CAP_USD):  # free hosting / explicitly disabled cap: no accounting needed
        return
    s = spent()
    if s + reserve > CAP_USD:
        raise BudgetExceeded(f"spent ${s:.2f} + reserve ${reserve:.2f} > cap ${CAP_USD:.2f}")


def record(kind, instance, cost, unknown=MAX_REPAIR_USD, tokens=None, duration_s=None, model=None):
    if cost is None or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        cost = unknown  # never record unknown spend as zero
    obj = {"kind": kind, "instance": instance, "cost": cost, "ts": time.time()}
    if tokens is not None:
        obj["tokens"] = tokens
    if duration_s is not None:
        obj["duration_s"] = duration_s
    if model is not None:
        obj["model"] = model
    with _lock:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(obj) + "\n")


# ---------- cache ----------
def key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def cached(kind, k, fn):
    """fn() -> result dict. Hit: cost 0, no budget check. A result is stored when it is clean, was really attempted
    (attempted=True: Claude actually started, so a re-run would re-bill even under free pricing), or cost money.
    An unbilled infra error (docker pull/cp before the agent starts) is not stored and may retry."""
    p = CACHE / kind / f"{k}.json"
    if p.exists():
        old = json.loads(p.read_text())
        return {**old, "cached": True, "orig_cost_usd": old.get("cost_usd"), "cost_usd": 0.0}
    res = fn()
    if not res.get("error") or res.get("attempted") or res.get("cost_usd", 0) > 0:
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
    """Remove every image ensure_image handed out and still tracked. Safe to call at any time; run_repair normally removes
    its own image, so this is a backstop for stops and crashes."""
    if KEEP_IMAGES:
        print("SSE_KEEP_IMAGES=1: keeping benchmark images")
        return 0
    with _img_lock:
        ids = sorted(_used_images)
        _used_images.clear()
    n = sum(remove_image(i) for i in ids)
    if ids:
        print(f"removed {n}/{len(ids)} benchmark images")
    return n


def _forget_image(iid):
    """Drop one image as soon as its repair is done, so a run holds at most WORKERS images (~15 GB), not the whole split."""
    with _img_lock:
        _used_images.discard(iid)
    remove_image(iid)


def ensure_image(iid):
    if free_gb() < MIN_FREE_GB:
        raise RuntimeError(f"only {free_gb():.1f} GB free; refusing to pull {iid} (SSE_MIN_FREE_GB={MIN_FREE_GB:.0f})")
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
FATAL_RE = re.compile(r"invalid x-api-key|invalid api key|authentication_error|api error: ?401|credit balance is too low|"
                      r"invalid proxy server token|incorrect api key|invalid_api_key|"
                      r"please run /login", re.I)
# Free hosted quota and model routing are transient: repair_pr retries with backoff instead of stopping the whole
# multi-hour run. A persistent 403 still stops the run after the bounded retries.
RATE_LIMIT_RE = re.compile(r"rate_limit_error|usage limit reached|too many requests|api error: ?429", re.I)
TRANSIENT_RE = re.compile(r"unrecognized_model|model not found|temporarily unavailable|asu error page|api error: ?403", re.I)
RATE_LIMIT_SLEEPS = (60, 120)  # seconds before the 1st and 2nd retry (rate limit or transient model routing)


def parse_claude(stdout):
    """Tolerant parse of `claude -p --output-format json`. Returns text, cost, ok, is_error, subtype and a `usage` dict
    (input/output/cache_creation/cache_read tokens). Cost is recomputed from the token usage at reviewer.PRICE_*: Claude Code's own
    total_cost_usd uses Anthropic prices and does not know this model. ok False (no usage) -> cost = MAX_REPAIR_USD."""
    out = {"text": "", "cost": MAX_REPAIR_USD, "ok": False, "is_error": False, "subtype": "",
           "usage": {k: 0 for k in USAGE_KEYS}}
    try:
        o = json.loads(stdout)
        if isinstance(o, list):
            o = [x for x in o if isinstance(x, dict) and x.get("type") == "result"][-1]
        out.update(text=str(o.get("result", "")), is_error=bool(o.get("is_error")), subtype=str(o.get("subtype") or ""))
        u = o["usage"]  # missing usage = unparseable for costing purposes (ok stays False)
        usage = {k: int(u.get(k) or 0) for k in USAGE_KEYS}
        tin = usage["input_tokens"] + usage["cache_creation_input_tokens"] + usage["cache_read_input_tokens"]
        cost = (tin * reviewer.PRICE_IN + usage["output_tokens"] * reviewer.PRICE_OUT) / 1e6
        assert math.isfinite(cost) and cost >= 0
        out.update(cost=float(cost), ok=True, usage=usage)
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
    """Vendor invocation plus --output-format json. Claude Code is pointed at the local proxy via a mode-600 env file (docker cp,
    owned by agent, sourced then deleted before claude starts). It holds only the per-run proxy token, never the ASU key."""
    _put(session, prompt, PROMPT_PATH)
    _put(session, f"export ANTHROPIC_BASE_URL='{PROXY_URL_IN_CONTAINER}' ANTHROPIC_AUTH_TOKEN='{PROXY_TOKEN}' "
                  f"ANTHROPIC_MODEL='{model}' ANTHROPIC_SMALL_FAST_MODEL='{model}' ANTHROPIC_DEFAULT_HAIKU_MODEL='{model}' "
                  f"ANTHROPIC_DEFAULT_SONNET_MODEL='{model}' ANTHROPIC_DEFAULT_OPUS_MODEL='{model}' "
                  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_TELEMETRY=1\n", KEY_PATH)
    session.run_command(f"chown agent:agent {PROMPT_PATH} {KEY_PATH} && chmod 600 {KEY_PATH}", timeout=10)
    if on_start:
        on_start()  # from here on spend is possible
    try:
        r = session.run_command(
            ["su", "-", "agent", "-s", "/bin/bash", "-c",
             f"cd /workspace && . {KEY_PATH} && rm -f {KEY_PATH} && cat {PROMPT_PATH} | {CLAUDE_BIN} -p "
             f"--dangerously-skip-permissions --model {model} --output-format json"],
            timeout=REPAIR_TIMEOUT)
    except TypeError:  # vendor bug: its timeout handler does bytes + str when the process wrote to stderr; treat as the timeout it is
        r = SimpleNamespace(returncode=-1, stdout="", stderr="\nCommand timed out (vendor handler crashed)", timed_out=True)
    session.run_command(f"rm -f {KEY_PATH}", timeout=10)
    return r


def run_repair(inst, findings, model=REPAIR_MODEL):
    """One container, one Claude Code invocation, score every Stage 4 test of the PR."""
    t0 = time.time()
    iid, tests = inst["instance_id"], stage4_tests(inst)
    invoked = False  # flips only right before the Claude run: earlier failures are free and safe to retry
    res = {"instance_id": iid, "repo": inst["repo"], "per_comment": [{"comment_index": ci, "passed": None} for ci, _, _ in tests],
           "agent_diff": "", "cost_usd": 0.0, "error": None, "duration_s": 0.0, "claude_version": None,
           "attempted": False, "rate_limited": False, "transient": False, "usage": {k: 0 for k in USAGE_KEYS}}

    def done(error=None, passed=None):
        res["attempted"] = invoked
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

    def mark():
        nonlocal invoked
        invoked = True
    img_ok = False
    try:
        ensure_image(iid)
        img_ok = True
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
            pip0 = session.run_command("pip install -e . --no-deps --quiet", timeout=PIP_TIMEOUT)
            if getattr(pip0, "timed_out", False):
                # The image already has the repository installed editable. A slow reinstall is setup, not a scored
                # failure; let the agent run and let the tests decide.
                print(f"pip install timed out ({PIP_TIMEOUT}s) on {iid}; continuing")
        prompt = build_tool_prompt(findings, inst["commit_to_review"]["patch_to_review"], inst["repo"])  # vendor prompt, unchanged
        r = invoke_claude(session, prompt, model, mark)  # sets `invoked` right before the Claude run; earlier failures cost $0
        write_json(RAW / f"{iid.replace('/', '__')}_{uuid.uuid4().hex[:8]}.json",
                   {"returncode": r.returncode, "timed_out": r.timed_out, "stdout": r.stdout, "stderr": r.stderr})
        p = parse_claude(r.stdout)
        res["usage"] = p["usage"]
        res["attempted"] = invoked
        err_text = " ".join([r.stderr or "", p["text"] if p["is_error"] else ""])
        res["rate_limited"] = bool(RATE_LIMIT_RE.search(err_text))
        res["transient"] = bool(TRANSIENT_RE.search(err_text))
        failed = p["is_error"] or r.returncode != 0
        hay = " ".join([p["text"] if p["is_error"] else "", r.stderr[:500], "" if p["ok"] else r.stdout[:500]])
        if failed and not r.timed_out and p["subtype"] != "error_max_budget_usd" and FATAL_RE.search(hay):
            res["attempted"] = invoked
            res["duration_s"] = round(time.time() - t0, 1)
            # parsed cost (maybe $0); MAX only if the CLI ran and output was unparseable
            record("repair", iid, p["cost"], tokens=p["usage"], duration_s=res["duration_s"], model=model)
            raise FatalRunError(f"Claude Code failed for {iid}, stopping the run: "
                                f"{scrub(' '.join([p['text'], p['subtype'], r.stderr[:200]]))[:300]}")
        # ponytail: a timeout is charged at the max and left as an error, so summaries count it failed (errors-as-failed) and the gate
        # excludes it; it is not scored on a partial diff. Score the diff instead if timeouts turn out common.
        res["cost_usd"] = MAX_REPAIR_USD if r.timed_out else p["cost"]  # unknown cost is charged at the max
        if r.timed_out or not p["ok"]:
            return done(error="claude timed out" if r.timed_out else f"claude output unparseable (rc={r.returncode})")
        diff_res = session.run_command("git diff", timeout=30)
        if getattr(diff_res, "timed_out", False):
            return done(error="git diff timed out after the agent ran")
        diff = diff_res.stdout
        res["agent_diff"] = scrub(diff)
        status_res = session.run_command("git status --porcelain", timeout=15)
        if getattr(status_res, "timed_out", False):
            return done(error="git status timed out after the agent ran")
        dirty = diff.strip() or status_res.stdout.strip()
        if failed and p["subtype"] != "error_max_budget_usd" and not dirty:
            # the agent did not really run: infra error, not a failed test
            return done(error=f"claude failed (rc={r.returncode}, subtype={p['subtype']}): {p['text'][:200]}")
        if not dirty:
            return done(passed=False)  # no changes = failed tests, not an infra error
        if lang == "python":
            pip1 = session.run_command("pip install -e . --no-deps --quiet", timeout=PIP_TIMEOUT)
            if getattr(pip1, "timed_out", False):
                print(f"pip install timed out ({PIP_TIMEOUT}s) on {iid} before tests; continuing")
        timed_out_test = False
        for pc, (ci, i, e) in zip(res["per_comment"], tests):
            ok, out = verify_with_test(session, e["test_code"], f"test_review_comment_{i}{EXT.get(lang, '.py')}", lang)
            if not ok and "Command timed out" in (out or ""):
                timed_out_test = True
                pc["passed"] = None  # infrastructure timeout: excluded, never a failed review
            else:
                pc["passed"] = ok
        res["attempted"] = invoked
        res["duration_s"] = round(time.time() - t0, 1)
        if timed_out_test:
            res["error"] = "test run timed out"
        return res
    except StopRun:
        res["attempted"] = invoked
        raise
    except Exception as e:
        res["attempted"] = invoked
        if invoked and not res["cost_usd"] and MAX_REPAIR_USD > 0:
            res["cost_usd"] = MAX_REPAIR_USD  # paid model: unknown cost after the agent started
        return done(error=f"{type(e).__name__}: {e}"[:500])
    finally:
        try:
            session.remove(force=True)
        finally:
            if img_ok and not KEEP_IMAGES:
                _forget_image(iid)  # bound disk to ~WORKERS images instead of the whole split


# ---------- budgeted, cached entry points ----------
def run_review(inst, skills, model=reviewer.MODEL, fresh=False, salt=None):
    """fresh=True or salt=<string> forces a new draw under a distinct cache key. A caller-supplied salt is deterministic:
    the same salt plus the same prompt reuses the stored draw, so a long run can resume without re-rolling results."""
    msgs = reviewer.build_messages(inst, skills)
    parts = [inst["instance_id"], json.dumps(msgs), model, reviewer.TEMPERATURE, reviewer.MAX_TOKENS, reviewer.RETRY_MAX_TOKENS]
    if fresh or salt:
        parts.append(str(salt) if salt else uuid.uuid4().hex)
    k = key("review", *parts)
    t0 = time.time()
    def go():
        check_budget(REVIEW_RESERVE_USD)
        return reviewer.review(inst, skills, model,
                               on_cost=lambda c, u=None: record("review", inst["instance_id"], c, tokens=u,
                                                                duration_s=round(time.time() - t0, 1), model=model))
    return cached("review", k, go)


def repair_pr(inst, findings, model=REPAIR_MODEL, fresh=False, salt=None):
    if not findings:  # nothing to apply: no container, no cost
        return run_repair(inst, findings, model)
    prompt = build_tool_prompt(findings, inst["commit_to_review"]["patch_to_review"], inst["repo"])

    def go():
        rate_retries, infra_retried = 0, False
        while True:
            check_budget(MAX_REPAIR_USD)
            r = run_repair(inst, findings, model)
            # each attempt is recorded exactly once, including rate-limited attempts (no double charge)
            record("repair", inst["instance_id"], r["cost_usd"], tokens=r.get("usage"),
                   duration_s=r.get("duration_s"), model=model)
            if r.get("error") and (r.get("rate_limited") or r.get("transient")
                                   or RATE_LIMIT_RE.search(str(r["error"])) or TRANSIENT_RE.search(str(r["error"]))):
                if rate_retries >= len(RATE_LIMIT_SLEEPS):
                    raise FatalRunError(f"transient/rate-limited failure on {inst['instance_id']} after "
                                        f"{rate_retries} retries: {scrub(str(r['error']))[:300]}")
                delay = RATE_LIMIT_SLEEPS[rate_retries]
                rate_retries += 1
                print(f"transient/rate-limited on {inst['instance_id']}; retry {rate_retries}/{len(RATE_LIMIT_SLEEPS)} in {delay}s")
                time.sleep(delay)
                continue
            if (r.get("error") and not r.get("attempted") and not r.get("cost_usd") and not infra_retried
                    and "no docker image" not in str(r.get("error"))):
                infra_retried = True  # unbilled infra error before the agent started (docker pull/cp): retry once
                continue
            return r

    parts = [inst["instance_id"], HARNESS_VERSION, prompt, model, MAX_REPAIR_USD]
    if fresh or salt:
        parts.append(str(salt) if salt else uuid.uuid4().hex)
    return cached("repair", key("repair", *parts), go)


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
    start_proxy()
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
