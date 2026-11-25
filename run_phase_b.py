"""Phase B: safe, resumable sequential runner for the gated evolution (global, then scoped).

Runs `evolve.py evolve` for each condition, one after the other, with the gate threshold read from
`results/calibration/summary.json` (PROTOCOL.md §9.1). It never touches the test partition, never starts the
LiteLLM proxy itself (evolve.py does), and refuses to start while another evolve.py/proxy is running or the
`runs/phase_b.lock` lock is live.

Usage (after calibration):
    uv run python run_phase_b.py --dry-run          # print the exact commands, run nothing
    uv run python run_phase_b.py                    # run global then scoped, logs under runs/logs/
    uv run python run_phase_b.py --conditions scoped --resume
    uv run python run_phase_b.py --tau 2 --dry-run  # override the calibrated threshold

Stdlib only: importing this module makes no Docker, LiteLLM, or network calls.
"""
import argparse, json, math, os, re, shlex, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
LOGS = RUNS / "logs"
LOCK = RUNS / "phase_b.lock"
CALIBRATION = ROOT / "results/calibration/summary.json"
EVO = ROOT / "results/evolution"

DEV_N, SEL_N = 87, 34          # eligible development / selection PRs (seeded, nested; PROTOCOL §7)
SEED = 0
MAX_ROUNDS = 5                 # protocol cap for gated conditions
CONDITIONS = ("global", "scoped")

# The two static baselines the gate compares against: file -> (expected partition, expected n_done/n_requested).
STATIC_FILES = {
    "results/static_development.json": ("development", DEV_N),
    "results/static_selection.json": ("selection", SEL_N),
}

ROUND_RE = re.compile(r"^round (\d+):")
DONE_RE = re.compile(r"^(\d+)/(\d+) rounds completed")
STOP_MARKERS = ("STOPPED:", "stop-after-rejections:")


# ---------- output helpers ----------
def say(msg=""):
    print(msg, flush=True)


def die(msg, code=2):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _display(path):
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ---------- preflight ----------
def resolve_tau(explicit):
    """Gate threshold: --tau, else results/calibration/summary.json. Never guess. Returns (tau, source)."""
    if explicit is not None:
        if explicit < 1:
            die(f"--tau {explicit} is below 1; the calibrated gate is floored at 1 (PROTOCOL.md §9.1)")
        return explicit, "cli --tau"
    if not CALIBRATION.exists():
        die(f"missing {_display(CALIBRATION)}: run the A/A calibration and `uv run python calibrate_gate.py` "
            f"first, or pass --tau explicitly. Refusing to guess a gate threshold.")
    try:
        summary = json.loads(CALIBRATION.read_text())
    except (OSError, json.JSONDecodeError) as ex:
        die(f"cannot read {_display(CALIBRATION)}: {ex}")
    if not isinstance(summary, dict):
        die(f"{_display(CALIBRATION)} is not a JSON object")
    tau = summary.get("tau")
    if not _is_number(tau):
        die(f"{_display(CALIBRATION)}: tau is missing or not numeric ({tau!r}); "
            f"re-run calibrate_gate.py (it derives tau) or pass --tau")
    if float(tau) != int(tau):
        die(f"{_display(CALIBRATION)}: tau={tau!r} is not an integer; evolve.py takes an integer gate")
    tau = int(tau)
    if tau < 1:
        die(f"{_display(CALIBRATION)}: tau={tau} is below the protocol floor of 1")
    gate = summary.get("gate")
    if not isinstance(gate, dict):
        die(f"{_display(CALIBRATION)}: gate is missing or not an object ({gate!r}); "
            f"expected {{'min_gain': {tau}, 'max_regress': 1}}")
    mg, mr = gate.get("min_gain"), gate.get("max_regress")
    if not _is_number(mg) or not _is_number(mr) or int(mg) != tau or int(mr) != 1 or float(mg) != float(tau) or float(mr) != 1:
        die(f"{_display(CALIBRATION)}: gate={gate!r} does not match the calibrated "
            f"{{'min_gain': {tau}, 'max_regress': 1}}; re-run calibrate_gate.py or pass --tau")
    # A stale/partial or hand-edited summary must not silently set the frozen gate.
    n_repeats = summary.get("n_repeats")
    if not isinstance(n_repeats, int) or isinstance(n_repeats, bool) or n_repeats < 5:
        die(f"{_display(CALIBRATION)}: n_repeats={n_repeats!r}; the protocol requires 5 A/A repeats")
    if summary.get("n_selection_prs") != SEL_N:
        die(f"{_display(CALIBRATION)}: n_selection_prs={summary.get('n_selection_prs')!r}; expected {SEL_N}")
    salts = summary.get("salts")
    if not isinstance(salts, list) or len(salts) < 5 or len(set(salts)) != len(salts):
        die(f"{_display(CALIBRATION)}: salts must be at least 5 distinct draws ({salts!r})")
    p95 = summary.get("net_abs_p95_primary")
    if not _is_number(p95):
        die(f"{_display(CALIBRATION)}: net_abs_p95_primary is missing/not numeric ({p95!r})")
    want = max(1, math.ceil(float(p95)))
    if tau != want:
        die(f"{_display(CALIBRATION)}: tau={tau} != max(1, ceil(net_abs_p95_primary={p95:.3f}))={want}")
    return tau, _display(CALIBRATION)


def validate_static_baselines(expected_salt="static-v1"):
    """Both static baselines must exist, be complete, and come from the requested salt (the calibration
    overwrites results/static_selection.json; the runner must not accept a calib-* draw as the baseline)."""
    checked = []
    for rel, (partition, want) in STATIC_FILES.items():
        p = ROOT / rel
        if not p.exists():
            die(f"missing {rel}: run the static baseline on the {partition} partition first "
                f"(README step 5/6). Refusing to start.")
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as ex:
            die(f"cannot read {rel}: {ex}")
        if not isinstance(d, dict):
            die(f"{rel} is not a JSON object")
        got_part = d.get("partition")
        if got_part == "test":
            die(f"{rel} is a TEST-partition result; the test partition is locked and never used for evolution")
        if got_part != partition:
            die(f"{rel}: partition is {got_part!r}, expected {partition!r}")
        if d.get("condition") not in (None, "static"):
            die(f"{rel}: condition is {d.get('condition')!r}, expected 'static'")
        n_done, n_req = d.get("n_done"), d.get("n_requested")
        for label, v in (("n_done", n_done), ("n_requested", n_req)):
            if not isinstance(v, int) or isinstance(v, bool):
                die(f"{rel}: {label} is missing or not an integer ({v!r})")
        if n_done != want or n_req != want:
            die(f"{rel}: n_done={n_done}, n_requested={n_req}; need {want}/{want}. "
                f"An incomplete baseline makes the gate uncalibrated; resume it first.")
        if d.get("salt") != expected_salt:
            die(f"{rel}: salt is {d.get('salt')!r}, expected {expected_salt!r}. The calibration writes "
                f"results/static_selection.json; restore the static-v1 baseline from "
                f"results/calibration/baseline_selection.json before running Phase B.")
        checked.append((rel, n_done, n_req))
    return checked


def validate_resume_state(condition, tau, salt):
    """Refuse a resume whose latest header used a different salt/gate, or whose latest run already stopped."""
    path = EVO / f"{condition}.jsonl"
    if not path.exists():
        return
    recs = []
    for line in path.read_text().splitlines():
        try:
            recs.append(json.loads(line))
        except Exception:
            continue
    idx = max((i for i, r in enumerate(recs) if "dev_ids" in r), default=None)
    if idx is None:
        return
    header = recs[idx]
    if header.get("salt") != salt:
        die(f"{_display(path)}: latest header salt={header.get('salt')!r}, requested {salt!r}; refusing --resume")
    gate = header.get("gate") or {}
    if int(gate.get("min_gain", -1)) != tau or int(gate.get("max_regress", -1)) != 1:
        die(f"{_display(path)}: latest header gate={gate!r}, requested min_gain={tau}, max_regress=1; refusing --resume")
    consecutive = 0
    for r in recs[idx + 1:]:
        if r.get("stopped") == "stop-after-rejections":
            die(f"{_display(path)}: latest run already stopped after consecutive rejections; refusing --resume")
        if "accept" in r and r.get("edits"):
            consecutive = 0 if r["accept"] else consecutive + 1
    if consecutive >= 2:
        die(f"{_display(path)}: latest run already has {consecutive} consecutive rejections; refusing --resume")


def parse_conditions(raw):
    items = [c.strip() for c in str(raw).split(",") if c.strip()]
    if not items:
        die("--conditions is empty; expected a comma list from global,scoped")
    bad = [c for c in items if c not in CONDITIONS]
    if bad:
        die(f"unknown condition(s) {bad}; allowed: {','.join(CONDITIONS)}")
    out = []
    for c in items:
        if c not in out:
            out.append(c)
    return out


# ---------- process / lock safety ----------
def _ps_rows():
    """(pid, command) rows from ps; stdlib only, no Docker calls."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as ex:
        warn(f"could not list processes ({ex}); relying on the lock file alone")
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1]))
    return rows


def find_conflicts():
    """Other evolve.py runs (calibration included) or a running LiteLLM proxy, excluding this process and its shell."""
    me = os.getpid()
    skip = {me, os.getppid()}
    hits = []
    for pid, cmd in _ps_rows():
        if pid in skip or "run_phase_b.py" in cmd:
            continue
        is_evolve = "evolve.py" in cmd and "run_phase_b.py" not in cmd
        is_proxy = "litellm" in cmd and "proxy.yaml" in cmd
        if is_evolve or is_proxy:
            hits.append((pid, cmd))
    return hits


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but owned by someone else
        return True
    except OSError:
        return False
    return True


def lock_state():
    """(live, pid, detail). A malformed lock counts as not live but is reported."""
    if not LOCK.exists():
        return False, None, "absent"
    try:
        d = json.loads(LOCK.read_text())
    except (OSError, json.JSONDecodeError):
        return False, None, "malformed"
    pid = d.get("pid") if isinstance(d, dict) else None
    if pid_alive(pid):
        return True, pid, "live"
    return False, pid, "stale"


def acquire_lock(meta):
    RUNS.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        live, pid, detail = lock_state()
        if live:
            holder = f" (pid {pid})" if pid else ""
            die(f"{_display(LOCK)} is live{holder}; another Phase B runner appears to be running. "
                f"If it is dead, remove the lock file.", 2)
        warn(f"replacing {detail} lock {_display(LOCK)}")
        try:
            LOCK.unlink()
        except OSError as ex:
            die(f"cannot remove {detail} lock {_display(LOCK)}: {ex}")
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            die(f"lost a race creating {_display(LOCK)}; another runner just started")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(meta, indent=2) + "\n")


def release_lock():
    try:
        if LOCK.exists():
            d = json.loads(LOCK.read_text())
            if isinstance(d, dict) and d.get("pid") == os.getpid():
                LOCK.unlink()
    except (OSError, json.JSONDecodeError):
        pass


# ---------- commands + execution ----------
def build_command(condition, tau, args):
    cmd = ["uv", "run", "python", "evolve.py", "evolve",
           "--condition", condition,
           "--dev-n", str(DEV_N),
           "--sel-n", str(SEL_N),
           "--rounds", str(args.rounds),
           "--gate-min-gain", str(tau),
           "--gate-max-regress", "1",
           "--stop-after-rejections", "2",
           "--seed", str(SEED),
           "--salt", args.salt]
    if args.resume:
        cmd.append("--resume")
    return cmd


def log_path_for(condition):
    return LOGS / f"phase_b_{condition}.log"


def run_command(condition, cmd, env):
    """Stream output to runs/logs/phase_b_<condition>.log and stdout. Return (rc, rounds_ran, last_round, stop)."""
    path = log_path_for(condition)
    LOGS.mkdir(parents=True, exist_ok=True)
    rounds_ran, last_round, stop = 0, 0, None
    with open(path, "a", encoding="utf-8") as log:
        header = (f"\n===== phase_b {datetime.now().isoformat(timespec='seconds')} condition={condition} "
                  f"pid={os.getpid()} =====\n$ {shlex.join(cmd)}\n")
        log.write(header)
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
                m = ROUND_RE.match(line)
                if m:
                    last_round = int(m.group(1))
                m = DONE_RE.match(line)
                if m:
                    rounds_ran = int(m.group(1))
                if any(x in line for x in STOP_MARKERS):
                    stop = line.strip()
            rc = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        finally:
            if proc.poll() is None:
                proc.kill()
    return rc, rounds_ran, last_round, stop


def command_line(cmd, env):
    return f"SSE_WORKERS={env['SSE_WORKERS']} {shlex.join(cmd)}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sequential, resumable Phase B gated-evolution runner.")
    ap.add_argument("--conditions", default="global,scoped",
                    help="Comma list from global,scoped, run in the given order (default: global,scoped)")
    ap.add_argument("--rounds", type=int, default=MAX_ROUNDS, help=f"Max evaluated rounds (default {MAX_ROUNDS}, protocol cap)")
    ap.add_argument("--workers", type=int, default=3, help="SSE_WORKERS for evolve.py (default 3; 4 is unsafe on 24 GB)")
    ap.add_argument("--salt", default="static-v1", help="Deterministic fresh-draw salt (default static-v1)")
    ap.add_argument("--tau", type=int, default=None,
                    help="Override the calibrated gate_min_gain (default: read results/calibration/summary.json)")
    ap.add_argument("--resume", action="store_true", help="Pass --resume to evolve.py (reuse recorded draws/snapshots)")
    ap.add_argument("--dry-run", action="store_true", help="Validate, print the exact commands, run nothing")
    args = ap.parse_args(argv)

    if args.rounds < 1 or args.rounds > MAX_ROUNDS:
        die(f"--rounds {args.rounds} outside 1..{MAX_ROUNDS} (protocol cap is {MAX_ROUNDS})")
    if args.workers < 1:
        die(f"--workers {args.workers} must be >= 1")
    if args.workers > 3:
        warn(f"--workers {args.workers}: harness.py notes 3 is safe on a 24 GB host and 4 is not")
    if not args.salt or not args.salt.strip():
        die("--salt must be a non-empty string (it makes the fresh draws reproducible)")
    conditions = parse_conditions(args.conditions)

    tau, tau_source = resolve_tau(args.tau)
    static_checked = validate_static_baselines(args.salt)
    if args.resume:
        for condition in conditions:
            validate_resume_state(condition, tau, args.salt)

    env = {**os.environ, "SSE_WORKERS": str(args.workers)}
    cmds = [(c, build_command(c, tau, args)) for c in conditions]

    say("Phase B runner — start")
    say(f"  root:        {ROOT}")
    say(f"  gate:        min_gain={tau} max_regress=1  (tau from {tau_source})")
    for rel, nd, nr in static_checked:
        say(f"  baseline:    {rel}  n_done={nd}/{nr}")
    say(f"  conditions:  {', '.join(conditions)} (sequential)")
    say(f"  flags:       rounds={args.rounds} seed={SEED} salt={args.salt} workers={args.workers} "
        f"stop-after-rejections=2 resume={'yes' if args.resume else 'no'}")
    say(f"  logs:        {(LOGS / 'phase_b_<condition>.log')}")

    if args.dry_run:
        live, pid, detail = lock_state()
        if live:
            warn(f"lock {_display(LOCK)} is live (pid {pid}); a real run would refuse to start")
        conflicts = find_conflicts()
        if conflicts:
            warn("another evolve.py/proxy is running; a real run would refuse to start: "
                 + "; ".join(f"pid {p}: {c[:120]}" for p, c in conflicts))
        say("  dry-run: nothing executed, no lock written")
        for condition, cmd in cmds:
            say(f"  [{condition}] {command_line(cmd, env)}")
        say("Phase B runner — end (dry-run)")
        return 0

    conflicts = find_conflicts()
    if conflicts:
        for pid, cmd in conflicts:
            print(f"  running: pid {pid}: {cmd}", file=sys.stderr, flush=True)
        die("refusing to start: another evolve.py/proxy is already running (see above). "
            "Wait for it or stop it; do not run Phase B concurrently.")
    live, pid, detail = lock_state()
    if live:
        holder = f" (pid {pid})" if pid else ""
        die(f"{_display(LOCK)} is live{holder}; another Phase B runner is running. "
            f"If it is dead, remove the lock file.")

    acquire_lock({"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds"),
                  "cwd": str(ROOT), "conditions": conditions, "rounds": args.rounds,
                  "tau": tau, "salt": args.salt, "workers": args.workers, "resume": args.resume})
    results, failed = [], None
    try:
        for condition, cmd in cmds:
            say("")
            say(f"[{condition}] {command_line(cmd, env)}")
            say(f"[{condition}] log: {_display(log_path_for(condition))}")
            rc, rounds_ran, last_round, stop = run_command(condition, cmd, env)
            rounds = rounds_ran or last_round
            results.append({"condition": condition, "rc": rc, "rounds": rounds,
                            "last_round": last_round, "stop": stop})
            if rc != 0:
                failed = {"condition": condition, "rc": rc, "rounds": rounds, "last_round": last_round}
                break
    except KeyboardInterrupt:
        say("")
        warn("interrupted; the child was terminated. Re-run with --resume to continue.")
        for r in results:
            say(f"  [{r['condition']}] rc={r['rc']} rounds={r['rounds']}/{args.rounds}"
                + (f" last_round={r['last_round']}" if r["last_round"] else ""))
        return 130
    finally:
        release_lock()

    say("")
    say("Phase B runner — end")
    for r in results:
        status = "ok"
        if r["stop"]:
            status = "stopped: " + r["stop"][:100]
        say(f"  [{r['condition']}] rc={r['rc']} rounds={r['rounds']}/{args.rounds}"
            + (f" last_round={r['last_round']}" if r["last_round"] else "") + f"  {status}")
    if failed:
        say(f"  overall: FAILED — {failed['condition']} exited {failed['rc']} "
            f"(last round {failed['last_round'] or 'not started'}); later conditions were not run")
        say("  fix the failure, then re-run with --resume")
        return 1
    say("  overall: OK — all requested conditions completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
