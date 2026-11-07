"""LiteLLM proxy hook. Qwen's chat template allows exactly one system message, at the very start. Claude Code sends a top-level
`system` (several blocks) plus system-role messages after the user turn. Fold them all into the one top-level `system` string
before the request is translated to the OpenAI format."""
from litellm.integrations.custom_logger import CustomLogger


def _text(x):
    return x if isinstance(x, str) else "\n\n".join(b.get("text", "") for b in x if isinstance(b, dict))


def merge_system_prompts(data):
    msgs = data.get("messages") or []
    extra = [_text(m["content"]) for m in msgs if m.get("role") == "system"]
    top = _text(data["system"]) if data.get("system") else ""
    if extra:
        data["messages"] = [m for m in msgs if m.get("role") != "system"]
    if extra or top:
        data["system"] = "\n\n".join(t for t in [top, *extra] if t)
    return data


class MergeSystem(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        return merge_system_prompts(data)


merge_system = MergeSystem()
