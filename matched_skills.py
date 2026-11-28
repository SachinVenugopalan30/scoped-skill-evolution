"""Content-matched skill libraries for the exploratory organization test (PROTOCOL.md A8).

  uv run python matched_skills.py            # writes results/skills/matched-{scoped,global}/round_0.json

For each PR type, the model reads the human review comments of the eligible DEVELOPMENT PRs of that type and writes
K short, general review skills. The same texts go into both libraries:
  * matched-scoped: each skill tagged with its PR type (a review retrieves its own type's skills);
  * matched-global: identical texts tagged "global" (every review retrieves the same newest skills that fit the budget).
Order interleaves the types so the global library's newest skills mix all three. Selection and test are never read.
"""
import json, re, sys

import evolve, harness, reviewer

K = 4
TYPES = ("bug_fix", "feature", "other")
NAMES = {"bug_fix": "bug-fix", "feature": "new-feature", "other": "other (refactoring, docs, tests, maintenance)"}
SYSTEM = ("You write review guidance for an automated code reviewer. You will see the comments human reviewers left on "
          "{kind} pull requests. Distil them into exactly {k} short, general, actionable review skills: what a reviewer "
          "of such pull requests should check. Each skill must be at most 55 words, must not name any project, "
          "repository, file, function or person, and must apply to other codebases. Reply with ONLY a JSON list of "
          "{k} strings.")


def dev_by_type():
    inst = harness.instances()
    by, _ = evolve._eligible("development")
    return {t: [inst[i] for i in by.get(t, [])] for t in TYPES}


def forbidden(insts):
    out = set()
    for i in insts:
        out |= {i["repo"].lower(), i["repo"].split("/")[-1].lower()}
        out |= {c["path"].lower() for c in i["reference_review_comments"] if c.get("path") and "/" in c["path"]}
    return {f for f in out if len(f) >= 5}


def generate(kind, insts, bad):
    comments = [c["text"].replace("\n", " ")[:300] for i in insts for c in i["reference_review_comments"]]
    user = "\n".join(f"- {c}" for c in comments)
    msgs = [{"role": "system", "content": SYSTEM.format(kind=NAMES[kind], k=K)}, {"role": "user", "content": user}]
    for attempt in range(3):
        raw = reviewer.litellm.completion(model=reviewer.MODEL, messages=msgs, temperature=0, max_tokens=900,
                                          **reviewer.llm_kwargs()).choices[0].message.content or ""
        skills = reviewer.extract_list(raw, "skills") or []
        skills = [" ".join(str(s).split()) for s in skills if isinstance(s, str)]
        leaks = sorted({f for s in skills for f in bad if f in s.lower()})
        if len(skills) == K and all(len(s.split()) <= 60 for s in skills) and not leaks:
            return skills
        msgs += [{"role": "assistant", "content": raw},
                 {"role": "user", "content": f"Invalid: need exactly {K} strings of at most 55 words with no project, "
                                             f"repository or file names{' (found: ' + ', '.join(leaks) + ')' if leaks else ''}. Try again."}]
    raise SystemExit(f"{kind}: no valid skills after 3 attempts")


def main():
    harness.setup()
    groups = dev_by_type()
    skills = {t: generate(t, groups[t], forbidden(groups[t])) for t in TYPES}
    scoped, glob = [], []
    for k in range(K):  # interleave types so newest-first global retrieval mixes them
        for t in TYPES:
            n = len(scoped)
            scoped.append({"id": f"m{n + 1}", "scope": t, "text": skills[t][k], "order": n})
            glob.append({"id": f"m{n + 1}", "scope": "global", "text": skills[t][k], "order": n})
    for cond, lib in (("matched-scoped", scoped), ("matched-global", glob)):
        evolve.save_snapshot(cond, 0, evolve.normalize_library(lib))
    for t in TYPES:
        print(t, "scoped retrieves", [s["id"] for s in evolve.retrieve(scoped, t, "matched-scoped")],
              "| global retrieves", [s["id"] for s in evolve.retrieve(glob, t, "matched-global")])
    print(json.dumps(skills, indent=1))


if __name__ == "__main__":
    sys.exit(main())
