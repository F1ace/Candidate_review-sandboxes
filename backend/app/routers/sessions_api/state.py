import json
import re
from typing import Any, Optional

from ... import models


_MAX_HISTORY_MESSAGES = 12


def _trim_memory_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _get_task_by_id(scenario: models.Scenario, task_id: str) -> Optional[dict[str, Any]]:
    for task in scenario.tasks or []:
        if task.get("id") == task_id:
            return task
    return None


def _control_state(session: models.Session, history: list[models.Message]) -> dict[str, Any]:
    intro_done = any(message.sender == "model" for message in history)
    scores = session.scores or {}
    task_status = {task_id: "scored" for task_id in scores.keys()}
    current_task = session.current_task_id or (session.scenario.tasks[0]["id"] if session.scenario.tasks else "none")
    awaiting_next = current_task in task_status
    return {
        "intro_done": intro_done,
        "current_task_id": current_task,
        "task_status": task_status,
        "awaiting_next_click": awaiting_next,
    }


def _semantic_memory(session: models.Session) -> dict[str, Any]:
    """Derive lightweight strengths/weaknesses from saved scores."""
    strengths: set[str] = set()
    weaknesses: set[str] = set()
    issues: list[str] = []
    scores = session.scores or {}

    for task in session.scenario.tasks or []:
        task_id = task.get("id")
        if not task_id or task_id not in scores:
            continue

        points = float(scores[task_id])
        max_points = float(task.get("max_points") or 1)
        ratio = points / max_points if max_points else 0.0
        topics = [str(topic).strip() for topic in (task.get("related_topics") or []) if str(topic).strip()]

        if ratio >= 0.8:
            strengths.update(topics)
        elif ratio <= 0.5:
            weaknesses.update(topics)
            for topic in topics[:2]:
                issues.append(f"weak:{topic}")

    return {
        "strengths": sorted(strengths),
        "weaknesses": sorted(weaknesses),
        "issues": issues[:4],
    }


def _episodic_memory(history: list[models.Message]) -> list[str]:
    events: list[str] = []
    for message in history[-20:]:
        if message.sender == "tool":
            events.append(f"tool:{_trim_memory_text(message.text, 90)}")
        elif message.sender == "system" and "error" in (message.text or "").lower():
            events.append(f"system:{_trim_memory_text(message.text, 90)}")
    return events[-4:]


def _convert_history(messages: list[models.Message]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []

    technical_prefixes = (
        "Code execution result for ",
        "run_code ->",
        "run_sql ->",
        "score_task ->",
        "LM service error:",
        "Ошибка сервиса LM Studio:",
    )

    for message in messages:
        text = (message.text or "").strip()
        if not text:
            continue

        if message.sender == "tool":
            continue

        if message.sender == "candidate":
            converted.append({"role": "user", "content": _trim_memory_text(text, 900)})
            continue

        if message.sender == "model":
            converted.append({"role": "assistant", "content": _trim_memory_text(text, 900)})
            continue

        if text.startswith(technical_prefixes):
            continue

        converted.append({"role": "system", "content": _trim_memory_text(text, 700)})

    if len(converted) > _MAX_HISTORY_MESSAGES:
        converted = converted[-_MAX_HISTORY_MESSAGES:]

    return converted


def _conversation_snapshot(session: models.Session, history: list[models.Message]) -> str:
    """Short explicit state that avoids replaying the whole conversation."""
    control = _control_state(session, history)
    semantic = _semantic_memory(session)
    episodic = _episodic_memory(history)

    last_user = next((message for message in reversed(history) if message.sender == "candidate"), None)
    last_model = next((message for message in reversed(history) if message.sender == "model"), None)

    scored_tasks = ",".join(sorted(control["task_status"].keys())) or "-"
    strengths = ", ".join(semantic.get("strengths", [])[:4]) or "-"
    weaknesses = ", ".join(semantic.get("weaknesses", [])[:4]) or "-"
    issues = "; ".join(semantic.get("issues", [])[:4]) or "-"
    recent_events = "; ".join(episodic) or "-"
    last_user_text = _trim_memory_text(last_user.text if last_user else "-", 180)
    last_model_text = _trim_memory_text(last_model.text if last_model else "-", 180)

    return (
        "<SNAPSHOT>"
        f"intro_done={str(control['intro_done']).lower()};"
        f"current_task={control['current_task_id']};"
        f"awaiting_next={str(control['awaiting_next_click']).lower()};"
        f"scored_tasks={scored_tasks};"
        f"strengths={strengths};"
        f"weaknesses={weaknesses};"
        f"issues={issues};"
        f"recent_events={recent_events};"
        f"last_user={last_user_text};"
        f"last_model={last_model_text};"
        "Продолжай интервью из текущего состояния. Не перезапускай уже завершённые части."
        "</SNAPSHOT>"
    )


def _theory_tasks(scenario: models.Scenario) -> list[dict[str, Any]]:
    return [task for task in (scenario.tasks or []) if task.get("type") == "theory"]


def _first_practice_task(scenario: models.Scenario) -> Optional[dict[str, Any]]:
    for task in (scenario.tasks or []):
        if task.get("type") in ("coding", "sql"):
            return task
    return None


def _theory_is_complete(session: models.Session) -> bool:
    theory_tasks = _theory_tasks(session.scenario)
    if not theory_tasks:
        return True
    scores = session.scores or {}
    return all(task.get("id") in scores for task in theory_tasks)


def _theory_summary_text(session: models.Session) -> str:
    theory_tasks = _theory_tasks(session.scenario)
    scores = session.scores or {}

    earned = 0.0
    maximum = 0.0
    for task in theory_tasks:
        task_id = task.get("id")
        maximum += float(task.get("max_points") or 10)
        earned += float(scores.get(task_id, 0))

    if maximum <= 0:
        return "Теоретический блок завершён."
    return f"Теоретический блок завершён: {earned:g}/{maximum:g}."


def advance_task_if_needed(session: models.Session, last_user_text: str) -> bool:
    """
    Advance to the next task only when the candidate explicitly writes "Следующее"
    and the current task already has a final score.
    """
    if not last_user_text:
        return False

    if last_user_text.strip().lower() != "следующее":
        return False

    tasks = session.scenario.tasks or []
    if not tasks:
        return False

    scores = session.scores or {}
    current_id = session.current_task_id or tasks[0].get("id")
    if not current_id or current_id not in scores:
        return False

    ids = [task.get("id") for task in tasks if task.get("id")]
    if current_id not in ids:
        return False

    current_index = ids.index(current_id)
    if current_index + 1 >= len(ids):
        return False

    session.current_task_id = ids[current_index + 1]
    return True
