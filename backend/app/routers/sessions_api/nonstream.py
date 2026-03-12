import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ... import models
from ...services.lm_client import lm_client
from .dispatch import _dispatch_tool_call, _aggregate_theory_intermediate_scores
from .practice import _score_feedback
from .prompting import _build_system_prompt, _extract_inline_tool_call
from .state import (
    _conversation_snapshot,
    _convert_history,
    _first_practice_task,
    _get_task_by_id,
    _theory_is_complete,
    _theory_summary_text,
)
from .tools import TOOLS

def _is_score_task_error(result: dict) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("ok") is False:
        return True
    if "error" in result and result["error"]:
        return True
    return False

def _looks_like_tool_dump(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    if low.startswith("score_task"):
        return True
    if "score_task" in low and "task_id" in low and "points" in low:
        return True
    if t.startswith("{") and t.endswith("}") and ("task_id" in low and "points" in low):
        return True
    return False


def _human_tool_error(result: dict) -> str:
    err = ""
    if isinstance(result, dict):
        err = result.get("error") or ""
    if not err:
        err = "неизвестная ошибка"
    return (
        "Не удалось записать оценку (score_task не принят системой).\n"
        f"Причина: {err}"
    )

def call_model(session_id: str, db: Session):
    """Non-streaming call (fallback)."""
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    history_db = (
        db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at)
        .all()
    )
    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).count() > 0
    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    messages.extend(_convert_history(history_db))

    try:
        first_resp = lm_client.chat(messages, tools=TOOLS)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls")

    # Fallback: если tool_calls нет, но модель напечатала tool-call текстом
    if not tool_calls:
        content = assistant_msg.get("content") or ""
        inline = _extract_inline_tool_call(content)
        if inline:
            tool_name, args = inline
            tool_calls = [{
                "id": "inline_toolcall",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }]
            # чтобы "<|channel|>commentary ..." не показывался в UI
            assistant_msg["content"] = None

    messages.append(assistant_msg)

    tool_results_db: list[models.Message] = []
    last_score_result: dict[str, Any] | None = None
    final_msg = assistant_msg

    MAX_SCORE_RETRIES = 2
    retries_left = MAX_SCORE_RETRIES

    while tool_calls:
        tool_messages = []
        score_task_failed = False
        score_task_error_text = ""
        score_task_max_points = None

        for tc in tool_calls:
            result = _dispatch_tool_call(session, tc, db)

            fname = tc["function"]["name"]
            if fname == "score_task":
                last_score_result = result

                # достанем max_points текущей задачи, чтобы подсказать модели допустимый диапазон
                try:
                    args_sc = json.loads(tc["function"].get("arguments", "{}"))
                except Exception:
                    args_sc = {}
                task_id_scored = args_sc.get("task_id")
                task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None
                if task_obj:
                    score_task_max_points = task_obj.get("max_points")

                if _is_score_task_error(result):
                    score_task_failed = True
                    score_task_error_text = result.get("error") or str(result)

            # theory -> final summary transition for non-stream
            try:
                args_sc = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args_sc = {}
            task_id_for_db = args_sc.get("task_id")

            task_obj2 = _get_task_by_id(session.scenario, task_id_for_db) if task_id_for_db else None
            if (
                task_obj2
                and task_obj2.get("type") == "theory"
                and isinstance(result, dict)
                and result.get("ok") is True
                and result.get("is_final") is True
                and _theory_is_complete(session)
            ):
                summary = _theory_summary_text(session)
                practice_task = _first_practice_task(session.scenario)
                aggregated = _aggregate_theory_intermediate_scores(session, db, task_id_for_db)

                practice_title = practice_task.get("title") if practice_task else ""
                practice_id = practice_task.get("id") if practice_task else ""
                practice_type = practice_task.get("type") if practice_task else ""
                practice_desc = ""
                if practice_task:
                    practice_desc = (
                        practice_task.get("description_for_candidate")
                        or practice_task.get("description")
                        or practice_task.get("prompt")
                        or ""
                    )

                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "ТЕОРИЯ ЗАВЕРШЕНА.\n"
                            "Промежуточные оценки и комментарии по вопросам уже сохранены системой.\n"
                            f"Количество промежуточных оценок: {aggregated['count']}\n"
                            f"Средняя промежуточная оценка: {aggregated['avg_points']}/10\n"
                            f"Промежуточные комментарии: {json.dumps(aggregated['comments'], ensure_ascii=False)}\n\n"
                            "Сейчас нужно написать пользователю ИТОГОВЫЙ человеко-понятный разбор по теоретическому блоку.\n"
                            "Не перечисляй сырые промежуточные оценки по каждому вопросу.\n"
                            "Нужно: 1) кратко подвести итог, 2) отметить сильные стороны, 3) назвать зоны роста, "
                            "4) показать финальную оценку по шкале 1..10, 5) сообщить, что продолжение интервью будет происходить во вкладке практического задания.\n\n"
                            f"{summary}\n"
                            f"Следующее практическое задание: {practice_id} {practice_title} (тип: {practice_type}).\n"
                            f"Описание: {practice_desc}\n"
                        ),
                    }
                )

            # tool result -> messages (как было)
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

            tool_results_db.append(
                models.Message(
                    session_id=session_id,
                    sender="tool",
                    text=f"{fname} -> {result}",
                    task_id=task_id_for_db,
                )
            )

        # приклеиваем tool-ответы в историю
        messages.extend(tool_messages)

        # Если score_task упал — заставляем модель повторить score_task корректно
        if score_task_failed and retries_left > 0:
            retries_left -= 1

            max_pts_text = ""
            if score_task_max_points is not None:
                max_pts_text = f" (max_points={score_task_max_points})"

            messages.append({
                "role": "system",
                "content": (
                    "score_task НЕ был принят системой.\n"
                    f"Причина: {score_task_error_text}{max_pts_text}\n"
                    "Требование: вызови score_task ещё раз с points строго в диапазоне [0, max_points] "
                    "и с непустым comment. НЕ пиши финальный текст пользователю, пока score_task не пройдет успешно."
                )
            })

            # просим модель снова сделать tool_calls
            retry_resp = lm_client.chat(messages, tools=TOOLS)
            final_msg = retry_resp["choices"][0]["message"]
            tool_calls = final_msg.get("tool_calls")

            # если она вместо tool_calls напечатала inline — используем твой механизм
            if not tool_calls:
                content = final_msg.get("content") or ""
                inline = _extract_inline_tool_call(content)
                if inline:
                    tool_name, args = inline
                    tool_calls = [{
                        "id": "inline_toolcall_retry",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }]
                    final_msg["content"] = None

            messages.append(final_msg)
            continue

        # Если score_task успешен (или score_task не было) — получаем финальное сообщение без tools
        final_resp = lm_client.chat(messages, tools=TOOLS)
        final_msg = final_resp["choices"][0]["message"]
        tool_calls = final_msg.get("tool_calls")

        # если внезапно опять tool_calls — цикл продолжит и обработает
        if not tool_calls:
            break

    # Если модель молчит после score_task — подставляем fallback feedback
    if (not final_msg.get("content")) and last_score_result:
        final_msg["content"] = _score_feedback(last_score_result)

    for tm in tool_results_db:
        db.add(tm)
    final_text = final_msg.get("content") or ""

    if last_score_result and _looks_like_tool_dump(final_text):
        if isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
            final_text = _score_feedback(last_score_result)
        else:
            final_text = _human_tool_error(last_score_result)
    final_msg["content"] = final_text

    if isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
        if not final_text or _looks_like_tool_dump(final_text):
            final_text = _score_feedback(last_score_result)

    db.add(
        models.Message(
            session_id=session_id,
            sender="model",
            text=final_msg.get("content") or "",
        )
    )
    db.commit()

    return {"message": final_msg}

