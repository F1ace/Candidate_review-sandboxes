import json
import re
from typing import Any, Optional

from ... import models


def _trim_prompt_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _question_text_for_prompt(question: Any) -> str:
    if isinstance(question, dict):
        raw = question.get("text") or question.get("question") or question.get("prompt") or ""
    else:
        raw = question
    return _trim_prompt_text(raw, limit=220)


def _task_outline_for_prompt(task: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(task.get("id") or ""),
        "type": str(task.get("type") or ""),
        "title": _trim_prompt_text(task.get("title") or task.get("name") or "", limit=80),
        "max_points": int(task.get("max_points", 10) or 10),
    }
    if payload["type"] == "theory":
        payload["question_count"] = len(task.get("questions") or [])
    return payload


def _current_task_payload_for_prompt(task: dict[str, Any] | None) -> dict[str, Any]:
    if not task:
        return {}

    payload = _task_outline_for_prompt(task)
    task_type = payload.get("type")

    if task_type == "theory":
        payload["questions"] = [
            question_text
            for question_text in (
                _question_text_for_prompt(item)
                for item in (task.get("questions") or [])
            )
            if question_text
        ][:12]
    else:
        brief = (
            task.get("description")
            or task.get("prompt")
            or task.get("text")
            or task.get("title")
            or ""
        )
        brief = _trim_prompt_text(brief, limit=700)
        if brief:
            payload["brief"] = brief

    return payload


def _build_system_prompt(session: models.Session, rag_available: bool) -> str:
    """Construct a compact instruction block for small-context models."""
    scenario = session.scenario
    role = session.role
    tasks = scenario.tasks or []
    current_task_id = session.current_task_id or (tasks[0].get("id") if tasks else "")
    current_task = next((task for task in tasks if task.get("id") == current_task_id), None)

    prompt_payload = {
        "task_outline": [_task_outline_for_prompt(task) for task in tasks[:8]],
        "current_task": _current_task_payload_for_prompt(current_task),
    }
    payload_text = json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"))

    return (
        "<SYSTEM>\n"
        "Ты AI-интервьюер для фиксированного сценария.\n"
        "Всегда отвечай кандидату на русском языке.\n"
        "Никогда не выводи <think>, JSON, сырые tool dump, channel tags и служебные ошибки.\n"
        f"Роль: {_trim_prompt_text(role.name, 60)} ({_trim_prompt_text(role.slug, 40)}). "
        f"Сценарий: {_trim_prompt_text(scenario.name, 80)} ({_trim_prompt_text(scenario.slug, 40)}). "
        f"Уровень: {_trim_prompt_text(scenario.difficulty, 20)}. "
        f"RAG доступен: {str(bool(rag_available)).lower()}. "
        f"Текущий task id: {current_task_id or 'none'}.\n"
        "Краткий payload сценария:\n"
        f"{payload_text}\n"
        "Правила:\n"
        "- Иди строго по порядку сценария и продолжай с текущей задачи.\n"
        "- Если вступление уже было показано, не повторяй его.\n"
        "- Начни с приветствия, объясни всё, что знаешь, роль, сценарий и цель интервью, затем сразу начни интервью.\n"
        "- Theory: задавай по одному вопросу в формате 'Вопрос i/N: ...'.\n"
        "- Theory: после каждого ответа сохраняй ровно один промежуточный score_task с is_final=false и question_index=i.\n"
        "- Theory: если доступен RAG, сначала вызывай rag_search, затем промежуточный score_task.\n"
        "- Theory: доступны только rag_search, web_search и score_task. Никогда не вызывай run_code и run_sql.\n"
        "- Theory: финальный score_task разрешён только после того, как оценены все вопросы теоретического блока.\n"
        "- После успешного финального theory score_task напиши обычное итоговое сообщение: теоретический блок завершён, с кратким описанием ответа кандидата, комментарии по каждому ответу, сильные стороны, зоны роста и точная оценка из points. Не добавляй переход к практике.\n"
        "- Coding: код кандидата должен быть в редакторе, а не в чате. Используй run_code, затем score_task.\n"
        "- SQL: используй run_sql, затем score_task.\n"
        "- Никогда не печатай кандидату аргументы tools и служебные маркеры.\n"
        "</SYSTEM>"
    )


def _normalize_lm_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(message) for message in messages]


def _strip_think(content: Optional[str]) -> str:
    if not content:
        return ""
    if "</think>" in content:
        return content.split("</think>", 1)[1].strip()
    return content.replace("<think>", "").strip()


def _strip_intro(text: str, intro_done: bool) -> str:
    """Cut repetitive greetings when intro already happened."""
    if not intro_done or not text:
        return text

    intro_patterns = [
        "привет",
        "добрый день",
        "добрый вечер",
        "здравствуйте",
        "давайте начнём",
        "давайте начнем",
        "начнём с теории",
        "начнем с теории",
    ]
    lowered = text.lower()
    if any(lowered.startswith(pattern) for pattern in intro_patterns):
        parts = text.split("\n", 1)
        return parts[1] if len(parts) > 1 else ""
    return text


_FIRST_TURN_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|добрый\s+(?:день|вечер)|привет(?:ствую)?|рад(?:а|ы)?\s+видеть)",
    re.IGNORECASE,
)


def _ensure_first_model_greeting(text: str, session: models.Session) -> str:
    cleaned = _strip_think(text or "").strip()
    if not cleaned or _FIRST_TURN_GREETING_RE.search(cleaned):
        return cleaned

    role_name = _trim_prompt_text(session.role.name, 60)
    scenario_name = _trim_prompt_text(session.scenario.name, 80)
    intro = (
        f"Здравствуйте! Проведу для вас интервью на роль {role_name} "
        f'по сценарию "{scenario_name}".'
    )
    return f"{intro}\n\n{cleaned}"


def _extract_inline_tool_call(
    content: str,
    allowed_tools: set[str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    if not content:
        return None

    match = re.search(
        r"to=([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)",
        content,
    )
    if not match:
        return None

    raw_name = match.group(1)
    tool_name = raw_name.split(".")[-1].strip()

    if allowed_tools is None:
        allowed_tools = set()
    if tool_name not in allowed_tools:
        return None

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    raw_json = content[start : end + 1]
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    return tool_name, payload


def _analyze_candidate_message(text: str) -> list[str]:
    """Detect low-signal or misplaced content in a candidate message."""
    flags: list[str] = []
    lowered = (text or "").strip().lower()

    if not lowered:
        flags.append("empty")
        return flags

    placeholder_phrases = [
        "ответ правильный",
        "решение корректное",
        "код верный",
        "(solution)",
    ]
    if any(phrase in lowered for phrase in placeholder_phrases):
        flags.append("placeholder")

    if len(lowered) < 40 and "select" not in lowered and "join" not in lowered:
        flags.append("too_short")

    roleplay_phrases = [
        "как модель",
        "как ассистент",
        "я бот",
    ]
    if any(phrase in lowered for phrase in roleplay_phrases):
        flags.append("roleplay")

    if "def " in lowered or "print(" in lowered or "import " in lowered:
        flags.append("code_in_chat")
    if "select " in lowered or " from " in lowered:
        flags.append("sql_in_chat")

    return flags
