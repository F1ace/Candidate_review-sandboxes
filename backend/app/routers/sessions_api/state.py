import json
from typing import Any, Optional

from ... import models
def _get_task_by_id(scenario: models.Scenario, task_id: str) -> Optional[dict[str, Any]]:
    for task in scenario.tasks or []:
        if task.get("id") == task_id:
            return task
    return None

def _control_state(session: models.Session, history: list[models.Message]) -> dict[str, Any]:
    intro_done = any(m.sender == "model" for m in history)
    scores = session.scores or {}
    task_status = {tid: "scored" for tid in scores.keys()}
    current_task = session.current_task_id or (session.scenario.tasks[0]["id"] if session.scenario.tasks else "нет")
    awaiting_next = current_task in task_status
    return {
        "intro_done": intro_done,
        "current_task_id": current_task,
        "task_status": task_status,
        "hint_count": {},
        "awaiting_next_click": awaiting_next,
        "code_submitted": {},
        "sql_submitted": {},
    }

def _semantic_memory(session: models.Session) -> dict[str, Any]:
    """Derive simple strengths/weaknesses from scores."""
    strengths: set[str] = set()
    weaknesses: set[str] = set()
    issues: list[dict[str, str]] = []
    scores = session.scores or {}
    for task in session.scenario.tasks or []:
        tid = task.get("id")
        if not tid or tid not in scores:
            continue
        pts = scores[tid]
        max_pts = task.get("max_points") or 1
        ratio = float(pts) / float(max_pts)
        topics = task.get("related_topics") or []
        if ratio >= 0.8:
            strengths.update(topics)
        elif ratio <= 0.5:
            weaknesses.update(topics)
            for t in topics:
                issues.append({"key": f"weak_{t}", "text": f"Низкий балл по теме {t}"})
    return {
        "strengths": list(strengths),
        "weaknesses": list(weaknesses),
        "issues": issues,
    }

def _episodic_memory(history: list[models.Message]) -> list[str]:
    events: list[str] = []
    for m in history[-60:]:
        if m.sender == "tool":
            events.append(f"tool:{m.text[:120]}")
        elif m.sender == "system" and "result" in m.text:
            events.append(f"system:{m.text[:120]}")
    return events[-30:]

def _convert_history(messages: list[models.Message]) -> list[dict[str, Any]]:
    converted = []
    for msg in messages:
        if msg.sender == "candidate":
            role = "user"
        elif msg.sender == "model":
            role = "assistant"
        else:
            role = "system"
        converted.append({"role": role, "content": msg.text})
    return converted

def _conversation_snapshot(session: models.Session, history: list[models.Message]) -> str:
    """Short, explicit state for the model to avoid repetition."""
    control = _control_state(session, history)
    sem = _semantic_memory(session)
    episodic = _episodic_memory(history)
    last_user = next((m for m in reversed(history) if m.sender == "candidate"), None)
    last_user_text = (last_user.text if last_user else "нет последних вопросов")[:200]
    last_model = next((m for m in reversed(history) if m.sender == "model"), None)
    last_model_text = (last_model.text if last_model else "нет")[:200]
    return (
        "<CONTROL_STATE>"
        f"<INTRO_DONE>{control['intro_done']}</INTRO_DONE>"
        f"<CURRENT_TASK_ID>{control['current_task_id']}</CURRENT_TASK_ID>"
        f"<AWAITING_NEXT_CLICK>{control['awaiting_next_click']}</AWAITING_NEXT_CLICK>"
        f"<TASK_STATUS>{json.dumps(control['task_status'], ensure_ascii=False)}</TASK_STATUS>"
        f"<HINT_COUNT>{json.dumps(control['hint_count'], ensure_ascii=False)}</HINT_COUNT>"
        f"<CODE_SUBMITTED>{json.dumps(control['code_submitted'], ensure_ascii=False)}</CODE_SUBMITTED>"
        f"<SQL_SUBMITTED>{json.dumps(control['sql_submitted'], ensure_ascii=False)}</SQL_SUBMITTED>"
        "</CONTROL_STATE>"
        "<SEMANTIC_MEMORY>"
        f"<STRENGTHS>{', '.join(sem.get('strengths', []))}</STRENGTHS>"
        f"<WEAKNESSES>{', '.join(sem.get('weaknesses', []))}</WEAKNESSES>"
        f"<ISSUES>{json.dumps(sem.get('issues', []), ensure_ascii=False)}</ISSUES>"
        "</SEMANTIC_MEMORY>"
        "<EPISODIC_MEMORY>"
        f"{json.dumps(episodic, ensure_ascii=False)}"
        "</EPISODIC_MEMORY>"
        f"<LAST_USER>{last_user_text}</LAST_USER>"
        f"<LAST_MODEL>{last_model_text}</LAST_MODEL>"
        "Не повторяй уже сказанное; продолжай диалог логично и не начинай новую задачу без явного перехода."
    )

def _theory_tasks(scenario: models.Scenario) -> list[dict[str, Any]]:
    return [t for t in (scenario.tasks or []) if t.get("type") == "theory"]

def _first_practice_task(scenario: models.Scenario) -> Optional[dict[str, Any]]:
    for t in (scenario.tasks or []):
        if t.get("type") in ("coding", "sql"):
            return t
    return None

def _theory_is_complete(session: models.Session) -> bool:
    theory = _theory_tasks(session.scenario)
    if not theory:
        return True
    scores = session.scores or {}
    return all((t.get("id") in scores) for t in theory)

def _theory_summary_text(session: models.Session) -> str:
    theory = _theory_tasks(session.scenario)
    scores = session.scores or {}

    earned = 0.0
    maximum = 0.0
    for t in theory:
        tid = t.get("id")
        maximum += 10.0
        earned += float(scores.get(tid, 0))

    if maximum <= 0:
        return f"Теория завершена. Оценено заданий: {sum(1 for t in theory if t.get('id') in scores)}/{len(theory)}."
    return f"Теория завершена. Итог: {earned:g}/{maximum:g}."

def advance_task_if_needed(session: models.Session, last_user_text: str) -> bool:
    """
    Если кандидат написал "Следующее" и текущая задача уже оценена —
    двигаем current_task_id на следующий task в сценарии.
    Возвращаем True если был переход.
    """
    if not last_user_text:
        return False

    if last_user_text.strip().lower() != "следующее":
        return False

    tasks = session.scenario.tasks or []
    if not tasks:
        return False

    scores = session.scores or {}

    # определяем текущую задачу
    current_id = session.current_task_id or tasks[0].get("id")
    if not current_id:
        return False

    # если текущая ещё НЕ оценена — не даём перейти
    if current_id not in scores:
        return False

    # ищем следующую
    ids = [t.get("id") for t in tasks if t.get("id")]
    if current_id not in ids:
        return False

    idx = ids.index(current_id)
    if idx + 1 >= len(ids):
        return False  # уже конец

    session.current_task_id = ids[idx + 1]
    return True

