import json
from typing import Any


def is_score_task_error(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("ok") is False:
        return True
    if "error" in result and result["error"]:
        return True
    return False


def looks_like_tool_dump(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True

    low = t.lower()

    # старый score_task-формат
    if low.startswith("score_task"):
        return True

    if "score_task" in low and "task_id" in low and "points" in low:
        return True

    if t.startswith("{") and t.endswith("}") and ("task_id" in low and "points" in low):
        return True

    # общий raw tool-call / pseudo tool-call
    if "to=functions." in low:
        return True
    if "to=score_task" in low:
        return True
    if "to=run_sql" in low:
        return True
    if "to=run_code" in low:
        return True
    if "to=rag_search" in low:
        return True
    if "to=web_search" in low:
        return True

    if "<channel>commentary" in low and "to=" in low:
        return True
    if "<|channel|>commentary" in low and "to=" in low:
        return True
    if "assistant<channel>commentary" in low and "to=" in low:
        return True
    if "<|start|>assistant" in low and "to=" in low:
        return True
    if "<|message|>{" in low and "to=" in low:
        return True

    # json-подобный tool payload
    if '"sql"' in low and '"task_id"' in low:
        return True
    if '"code"' in low and '"task_id"' in low:
        return True
    if '"query"' in low and '"task_id"' in low:
        return True

    return False


def attach_inline_tool_call(
    assistant_msg: dict[str, Any],
    tool_name: str,
    args: dict[str, Any],
    *,
    tool_call_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tool_calls = [
        {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }
    ]

    fixed_msg = dict(assistant_msg or {})
    fixed_msg["role"] = "assistant"
    fixed_msg["content"] = None
    fixed_msg["tool_calls"] = tool_calls

    return fixed_msg, tool_calls
