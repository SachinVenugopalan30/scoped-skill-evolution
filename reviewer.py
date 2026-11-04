"""LLM reviewer: PR title/body/diff + skill texts -> <=5 validated findings. No gold data ever enters the prompt."""
import json, math
import litellm

MODEL = "claude-sonnet-4-6"
MAX_FINDINGS = 5
MAX_TOKENS, RETRY_MAX_TOKENS, TEMPERATURE = 3000, 6000, 0
MAX_DIFF_CHARS = 120_000  # ponytail: hard char cap on the diff (~30k tokens); chunk or summarize per file if big PRs matter
PRICE_IN, PRICE_OUT = 3.0, 15.0  # $/MTok fallback when litellm has no price for the model


def cost_of(resp):
    """Dollar cost of a litellm response; never 0 for a call that used tokens."""
    try:
        c = litellm.completion_cost(completion_response=resp)
        if c and math.isfinite(c) and c > 0:
            return float(c)
    except Exception:
        pass
    u = resp.usage
    return (u.prompt_tokens * PRICE_IN + u.completion_tokens * PRICE_OUT) / 1e6


def build_messages(instance, skills):
    """Only title, body and the diff the vendor shows the repair agent (patch_to_review)."""
    diff = instance["commit_to_review"]["patch_to_review"][:MAX_DIFF_CHARS]
    sk = "".join(f"\n- {s}" for s in skills) or "\n(none)"
    system = ("You are an expert code reviewer. Read the pull request and report concrete defects that a reviewer "
              "would ask the author to fix.\n\nReview guidance:" + sk + "\n\n"
              f"Output ONLY a JSON list of at most {MAX_FINDINGS} findings, no prose. Each finding is an object with "
              '"file" (path as in the diff), "line" (int) or "start_line" and "end_line" (ints), '
              '"issue_header" (short title), "issue_content" (what is wrong and how to fix it).')
    user = f"# Title\n{instance['title']}\n\n# Description\n{instance['body']}\n\n# Diff\n```\n{diff}\n```"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _pos(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def _text(x):
    return isinstance(x, str) and x.strip() != ""


def extract_list(raw, key=None):
    """First JSON list in raw (or the list under `key` of the first JSON object); None if there is none."""
    i = min((raw.find(c) for c in "[{" if c in raw), default=-1)
    if i < 0:
        return None
    try:  # only the first bracket counts: a truncated outer list must not yield a stray inner one
        obj, _ = json.JSONDecoder().raw_decode(raw[i:])
    except ValueError:
        return None
    obj = obj.get(key) if isinstance(obj, dict) else obj
    return obj if isinstance(obj, list) else None


def parse_findings(raw):
    """Returns validated list (<=5), or None if no JSON list could be found."""
    obj = extract_list(raw, "findings")
    if obj is None:
        return None
    out = []
    for f in obj:
        if not (isinstance(f, dict) and _text(f.get("file")) and _text(f.get("issue_header"))
                and _text(f.get("issue_content"))):
            continue
        g = {"file": f["file"], "issue_header": f["issue_header"], "issue_content": f["issue_content"]}
        if _pos(f.get("line")):
            g["line"] = f["line"]
        elif _pos(f.get("start_line")) and _pos(f.get("end_line")):
            g["start_line"], g["end_line"] = f["start_line"], f["end_line"]
        else:
            continue
        out.append(g)
    return out[:MAX_FINDINGS]


def review(instance, skills, model=MODEL, on_cost=None):
    msgs, cost, usage, err, findings = build_messages(instance, skills), 0.0, {"prompt_tokens": 0, "completion_tokens": 0}, None, []
    for attempt in range(2):  # unparseable output is retried once, with a different request
        if attempt:
            msgs = msgs[:-1] + [{**msgs[-1], "content": msgs[-1]["content"] + "\n\nYour previous reply was not valid JSON. "
                                 "Reply with valid JSON only (a short list), keep it shorter."}]
        resp = litellm.completion(model=model, messages=msgs, temperature=TEMPERATURE, num_retries=2,
                                  max_tokens=RETRY_MAX_TOKENS if attempt else MAX_TOKENS)
        c = cost_of(resp)
        cost += c
        if on_cost:
            on_cost(c)  # spend is recorded per attempt, so a failure later cannot lose it
        usage["prompt_tokens"] += resp.usage.prompt_tokens
        usage["completion_tokens"] += resp.usage.completion_tokens
        parsed = parse_findings(resp.choices[0].message.content or "")
        if parsed is not None:
            return {"findings": parsed, "cost_usd": cost, "usage": usage}
        err = "unparseable reviewer output"
    return {"findings": [], "cost_usd": cost, "usage": usage, "error": err}
