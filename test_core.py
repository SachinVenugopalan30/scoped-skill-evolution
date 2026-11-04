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


if __name__ == "__main__":
    test_census(); test_splits(); test_reproducible()
    print("all tests passed")
