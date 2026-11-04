"""Census, PR types, repo-disjoint splits for the c-CRAB stage-4 set. Stdlib only."""
import json, random, time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
FUNNEL = ROOT / "vendor/c-crab/results_pipeline_funnel"
STAGES = ["stage0_full_test_split", "stage1_comment_filter", "stage2_docker_image",
          "stage3_testgen_verified", "stage4_agent_resolved"]
TYPE_MAP = {"Bug Fixes": "bug_fix", "New Feature Additions": "feature"}  # everything else -> other
TARGETS = {"development": (92, 112, 25), "selection": (37, 47, 20), "test": (55, 75, 22)}  # PRs, comments, repos
ALGO = ("random.Random(seed).shuffle(sorted repos); greedy fill in order development, selection, test: "
        "put repo in first partition whose PR, comment and repo caps it does not exceed; accept iff all "
        "targets hit exactly and every partition has all 3 PR types")


def load(stage):
    return [json.loads(l) for l in open(FUNNEL / f"{stage}.jsonl")]


def pr_type(r):
    return TYPE_MAP.get(r["metadata"]["problem_domain"], "other")


def census():
    out = {}
    for s in STAGES:
        rows = load(s)
        out[s] = {"instances": len(rows), "comments": sum(len(r["reference_review_comments"]) for r in rows)}
    rows = load(STAGES[-1])
    out["stage4_repos"] = len({r["repo"] for r in rows})
    return out


def repo_stats(rows):
    st = defaultdict(lambda: [0, 0, Counter()])
    for r in rows:
        s = st[r["repo"]]
        s[0] += 1; s[1] += len(r["reference_review_comments"]); s[2][pr_type(r)] += 1
    return st


def try_seed(seed, st):
    repos = sorted(st)
    random.Random(seed).shuffle(repos)
    parts = list(TARGETS)
    cur = {p: [0, 0, 0, Counter()] for p in parts}
    assign = {}
    for repo in repos:
        n, c, t = st[repo]
        for p in parts:
            cap = TARGETS[p]
            if cur[p][0] + n <= cap[0] and cur[p][1] + c <= cap[1] and cur[p][2] + 1 <= cap[2]:
                cur[p][0] += n; cur[p][1] += c; cur[p][2] += 1; cur[p][3].update(t)
                assign[repo] = p
                break
        else:
            return None
    for p in parts:
        if tuple(cur[p][:3]) != TARGETS[p] or len(cur[p][3]) < 3:
            return None
    return assign


def make_splits(rows, seed):
    """Deterministic given seed. Returns splits dict or None."""
    assign = try_seed(seed, repo_stats(rows))
    return None if assign is None else build(rows, assign, seed)


def build(rows, assign, seed):
    ids = {p: [] for p in TARGETS}
    for r in rows:
        ids[assign[r["repo"]]].append(r["instance_id"])
    summ = {}
    for p in TARGETS:
        rs = [r for r in rows if assign[r["repo"]] == p]
        summ[p] = {"prs": len(rs), "comments": sum(len(r["reference_review_comments"]) for r in rs),
                   "repos": len({r["repo"] for r in rs}), "types": dict(Counter(pr_type(r) for r in rs))}
    return {"seed": seed, "algorithm": ALGO, "targets": TARGETS, "repo_assignment": assign,
            "instance_ids": ids, "summary": summ}


def search(rows, limit=1_000_000, seconds=1800):
    t0 = time.time()
    st = repo_stats(rows)
    for seed in range(limit):
        if try_seed(seed, st):
            return seed
        if time.time() - t0 > seconds:
            break
    return None


if __name__ == "__main__":
    (ROOT / "results").mkdir(exist_ok=True)
    c = census()
    json.dump(c, open(ROOT / "results/census.json", "w"), indent=2)
    print(json.dumps(c, indent=2))
    rows = load(STAGES[-1])
    print("problem_domain:", Counter(r["metadata"]["problem_domain"] for r in rows))
    print("types:", Counter(pr_type(r) for r in rows))
    seed = search(rows)
    print("seed:", seed)
    if seed is not None:
        s = make_splits(rows, seed)
        json.dump(s, open(ROOT / "results/splits.json", "w"), indent=2)
        print(json.dumps(s["summary"], indent=1))
