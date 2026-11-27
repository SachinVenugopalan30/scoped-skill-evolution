"""Offline category labels for DEVELOPMENT reference comments (PROTOCOL.md A5, exploratory rich-feedback arms).

  uv run python classify_gold.py                 # label all eligible development comments -> results/gold_categories_dev.json
  uv run python classify_gold.py --sheet         # 30 seeded comments, blank labels, for the team's blind labelling
  uv run python classify_gold.py --kappa a.csv [b.csv]   # Cohen's kappa: each sheet vs the model, and a vs b

Only the development partition is read. Selection and test comments are never classified or opened.
"""
import argparse, csv, json, random, sys
from collections import Counter

import evolve, harness, reviewer

CATEGORIES = ("correctness/logic", "error handling", "input validation", "API/contract misuse", "tests/coverage",
              "performance", "concurrency/resources", "maintainability/style")
OUT = evolve.GOLD_CATEGORIES
SHEET = evolve.ROOT / "report/kappa_sheet.csv"
SYSTEM = ("Classify a code review comment by the kind of concern it raises. Answer with exactly one category from this "
          "list and nothing else:\n" + "\n".join(f"- {c}" for c in CATEGORIES) +
          "\nUse maintainability/style for naming, readability, duplication, docs or refactoring requests. "
          'Reply as JSON: {"category": "<one of the list>"}')


def dev_comments():
    inst = harness.instances()
    for iid in evolve.sample("development", 10 ** 9, 0):  # eligible development PRs only
        for k, c in enumerate(inst[iid]["reference_review_comments"]):
            yield iid, k, c, inst[iid]


def classify(text):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text[:4000]}]
    for _ in range(2):
        resp = reviewer.litellm.completion(model=reviewer.MODEL, messages=msgs, temperature=0, max_tokens=40,
                                           **reviewer.llm_kwargs())
        raw = resp.choices[0].message.content or ""
        try:
            cat = json.loads(raw[raw.find("{"): raw.rfind("}") + 1]).get("category")
        except ValueError:
            cat = next((c for c in CATEGORIES if c.lower() in raw.lower()), None)
        if cat in CATEGORIES:
            return cat
    return "unlabelled"


def kappa(a, b):
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--kappa", nargs="+")
    a = ap.parse_args()
    if a.kappa:
        model = json.loads(OUT.read_text())
        sheets = [{(r["instance_id"], int(r["comment_index"])): r["label"].strip() for r in csv.DictReader(open(f))}
                  for f in a.kappa]
        keys = sorted(set.intersection(*[set(k for k, v in s.items() if v) for s in sheets]))
        for f, s in zip(a.kappa, sheets):
            print(f"{f} vs model: kappa={kappa([s[k] for k in keys], [model[k[0]][k[1]] for k in keys]):.3f} (n={len(keys)})")
        if len(sheets) == 2:
            print(f"{a.kappa[0]} vs {a.kappa[1]}: kappa={kappa([sheets[0][k] for k in keys], [sheets[1][k] for k in keys]):.3f}")
        return
    rows = list(dev_comments())
    if a.sheet:
        pick = random.Random(572).sample(rows, 30)
        SHEET.parent.mkdir(exist_ok=True)
        with open(SHEET, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["instance_id", "comment_index", "comment_text", "label"])
            for iid, k, c, _ in pick:
                w.writerow([iid, k, c["text"], ""])
        print(f"wrote {SHEET} (30 rows; label with one of: {', '.join(CATEGORIES)})")
        return
    harness.setup()
    out = {}
    for iid, k, c, _ in rows:
        out.setdefault(iid, []).append(classify(c["text"]))
    harness.write_json(OUT, out)
    counts = Counter(v for vs in out.values() for v in vs)
    print(f"labelled {sum(counts.values())} comments in {len(out)} PRs -> {OUT}: {dict(counts.most_common())}")


if __name__ == "__main__":
    sys.exit(main())
