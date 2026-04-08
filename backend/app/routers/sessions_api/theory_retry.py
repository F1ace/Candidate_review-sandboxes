from __future__ import annotations

import json
import re
from typing import Any

from ... import models
from .state import _get_task_by_id
from .tool_errors import (
    THEORY_COMMENT_EMPTY,
    THEORY_COMMENT_TEMPLATE,
    THEORY_COMMENT_TOO_SHORT,
    THEORY_COMMENT_TRUNCATED,
    THEORY_FINAL_COMMENTS_REQUIRED,
    THEORY_FINAL_COMMENTS_TEXTUAL_SCORE,
    THEORY_FINAL_TEXTUAL_SCORE,
)
from .tools import TOOLS


_THEORY_SCORE_RANGE_RE = re.compile(r"Theory score should be within \[1,\s*\d+\]")


def is_retryable_theory_score_error(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is True:
        return False

    err = (result.get("error") or "").strip()
    return err in {
        THEORY_COMMENT_EMPTY,
        THEORY_COMMENT_TOO_SHORT,
        THEORY_COMMENT_TRUNCATED,
        THEORY_COMMENT_TEMPLATE,
    } or _THEORY_SCORE_RANGE_RE.search(err) is not None


def is_retryable_final_theory_score_error(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is True:
        return False

    err = (result.get("error") or "").strip()
    return err in {
        THEORY_FINAL_TEXTUAL_SCORE,
        THEORY_FINAL_COMMENTS_REQUIRED,
        THEORY_FINAL_COMMENTS_TEXTUAL_SCORE,
    }


def score_task_only_tools() -> list[dict[str, Any]]:
    return [
        tool for tool in TOOLS
        if tool.get("function", {}).get("name") == "score_task"
    ]


def force_pending_theory_intermediate_score(
    assistant_msg: dict[str, Any],
    *,
    task_id: str,
    question_index: int,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    tool_calls = assistant_msg.get("tool_calls")
    if not tool_calls:
        return assistant_msg, tool_calls

    first = tool_calls[0]
    function = first.get("function") or {}
    name = function.get("name") or ""
    if isinstance(name, str) and "." in name:
        name = name.split(".")[-1]
    if name != "score_task":
        return assistant_msg, tool_calls

    try:
        args = json.loads(function.get("arguments") or "{}")
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}

    args["task_id"] = task_id
    args["question_index"] = question_index
    args["is_final"] = False

    function["arguments"] = json.dumps(args, ensure_ascii=False)
    first["function"] = function
    tool_calls[0] = first
    assistant_msg["tool_calls"] = tool_calls
    return assistant_msg, tool_calls


def force_final_theory_score(
    assistant_msg: dict[str, Any],
    *,
    task_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    tool_calls = assistant_msg.get("tool_calls")
    if not tool_calls:
        return assistant_msg, tool_calls

    first = tool_calls[0]
    function = first.get("function") or {}
    name = function.get("name") or ""
    if isinstance(name, str) and "." in name:
        name = name.split(".")[-1]
    if name != "score_task":
        return assistant_msg, tool_calls

    try:
        args = json.loads(function.get("arguments") or "{}")
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}

    args["task_id"] = task_id
    args["is_final"] = True
    args["question_index"] = None

    function["arguments"] = json.dumps(args, ensure_ascii=False)
    first["function"] = function
    tool_calls[0] = first
    assistant_msg["tool_calls"] = tool_calls
    return assistant_msg, tool_calls


def resolve_current_task_id(session: models.Session, db) -> str | None:
    if session.current_task_id:
        return session.current_task_id

    tasks = session.scenario.tasks or []
    if not tasks:
        return None

    for task in tasks:
        task_id = task.get("id")
        if not task_id:
            continue

        final_score_exists = (
            db.query(models.Score)
            .filter(
                models.Score.session_id == session.id,
                models.Score.task_id == task_id,
                models.Score.is_final.is_(True),
            )
            .first()
            is not None
        )
        if not final_score_exists:
            return task_id

    return tasks[0].get("id")


def has_unscored_answer_for_current_theory_question(
    session: models.Session,
    db,
) -> tuple[bool, str | None, int | None]:
    current_task_id = resolve_current_task_id(session, db)
    if not current_task_id:
        return False, None, None

    task_obj = _get_task_by_id(session.scenario, current_task_id)
    if not task_obj or task_obj.get("type") != "theory":
        return False, current_task_id, None

    questions = task_obj.get("questions") or []
    total = len(questions)
    if not total:
        return False, current_task_id, None

    history = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )

    current_question_index = None
    for message in reversed(history):
        if message.sender != "model":
            continue
        text = (message.text or "").strip()
        match = re.search(r"(?im)вопрос\s+(\d+)\s*/\s*(\d+)", text)
        if match:
            current_question_index = int(match.group(1))
            break

    if not current_question_index:
        return False, current_task_id, None

    last_candidate = None
    for message in reversed(history):
        if message.sender == "candidate" and (message.text or "").strip():
            last_candidate = message
            break

    if not last_candidate:
        return False, current_task_id, current_question_index

    existing = (
        db.query(models.Score)
        .filter(
            models.Score.session_id == session.id,
            models.Score.task_id == current_task_id,
            models.Score.is_final.is_(False),
            models.Score.question_index == current_question_index,
        )
        .first()
    )

    needs_score = existing is None
    return needs_score, current_task_id, current_question_index


def build_theory_comment_retry_message(
    *,
    task_id: str,
    question_index: int,
    error_text: str,
    max_points: int | None = None,
) -> str:
    max_points = int(max_points or 10)
    return (
        f"Предыдущий промежуточный score_task для theory-вопроса question_index={question_index} был отклонён backend.\n"
        f"Причина: {error_text or 'unknown error'}\n\n"
        "Сейчас нужно НЕМЕДЛЕННО повторить только tool score_task.\n"
        "Исправь points и comment так, чтобы вызов был корректным.\n\n"
        "Не меняй:\n"
        f"- task_id={task_id}\n"
        f"- question_index={question_index}\n"
        "- is_final=false\n"
        f"- points должны оставаться в диапазоне [1, {max_points}]\n"
        "- даже если ответ кандидата полностью неверный, для theory нельзя ставить 0 или отрицательное значение: минимум равен 1\n\n"
        "Требования к comment:\n"
        "- 2-4 законченных предложения\n"
        "- на русском языке\n"
        "- без обрыва на двоеточии, тире, запятой или точке с запятой\n"
        "- кратко объясни, почему выставлены эти баллы\n"
        "- желательно использовать формат: 'Верно: ... Не хватает: ... Ошибка/сомнение: ...'\n"
        "- не копируй пустой шаблон вроде 'Верно / Не хватает / Ошибка/сомнение'\n"
        "- заполни каждую часть конкретикой по ответу кандидата\n"
        "- укажи минимум одну конкретную ошибку или один конкретный пробел в ответе\n\n"
        "Нельзя:\n"
        "- писать обычный текст кандидату\n"
        "- задавать следующий вопрос\n"
        "- вызывать другой инструмент\n\n"
        "Верни только один tool call score_task."
    )


def build_final_theory_comment_retry_message(
    *,
    task_id: str,
    error_text: str,
    max_points: int | None = None,
) -> str:
    max_points = int(max_points or 10)
    return (
        "Предыдущий ФИНАЛЬНЫЙ score_task для theory был отклонён backend.\n"
        f"Причина: {error_text or 'unknown error'}\n\n"
        "Сейчас нужно НЕМЕДЛЕННО повторить только tool score_task.\n"
        "Исправь поля comment и comments.\n\n"
        "Не меняй:\n"
        f"- task_id={task_id}\n"
        "- is_final=true\n"
        "- question_index=null\n"
        f"- points должны оставаться в диапазоне [1, {max_points}] и соответствовать итоговой оценке блока\n\n"
        "Требования к финальному payload:\n"
        "- comment: общий качественный итог по всему theory-блоку на русском языке\n"
        "- comment: без числовой оценки в тексте\n"
        "- нельзя писать '7/10', '6 из 10', 'ставлю 7', 'оценка 5'\n"
        "- comments: обязателен список комментариев по каждому вопросу текущей theory-задачи в порядке вопросов\n"
        "- comments: каждый элемент должен быть непустым и без числовой оценки текстом\n"
        "- нельзя писать внутри comments формулировки вроде '3 балла за вопрос', 'оценка 4/10'\n"
        "- comment и comments должны суммировать весь theory-блок, а не только последний вопрос\n\n"
        "Нельзя:\n"
        "- писать обычный текст кандидату\n"
        "- задавать следующий вопрос\n"
        "- вызывать другой инструмент\n\n"
        "Верни только один tool call score_task."
    )
