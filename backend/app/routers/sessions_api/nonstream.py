import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ... import models
from ...services.lm_client import lm_client
from ...services.theory_rag import (
    detect_current_theory_question_index,
    ensure_theory_validation,
    format_theory_validation_message,
)
from .dispatch import _dispatch_tool_call, _aggregate_theory_intermediate_scores
from .practice import _score_feedback
from .prompting import _build_system_prompt, _extract_inline_tool_call
from .state import (_conversation_snapshot, _convert_history, _first_practice_task, _get_task_by_id, _theory_is_complete, _theory_summary_text,)
from .tool_call_utils import (
    attach_inline_tool_call as _attach_inline_tool_call,
    is_score_task_error as _is_score_task_error,
    looks_like_tool_dump as _looks_like_tool_dump,
)
from .tools import TOOLS

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

def _should_allow_final_theory_score_tool(
    session: models.Session,
    db,
    task_id: str | None,
    score_result_payload: dict[str, Any] | None,
) -> bool:
    if not task_id:
        return False

    if not isinstance(score_result_payload, dict):
        return False

    if score_result_payload.get("ok") is not True:
        return False

    if bool(score_result_payload.get("is_final")):
        return False

    task_obj = _get_task_by_id(session.scenario, task_id)
    if not task_obj or task_obj.get("type") != "theory":
        return False

    aggregated = _aggregate_theory_intermediate_scores(session, db, task_id)
    missing_questions = aggregated.get("missing_questions") or []

    return len(missing_questions) == 0

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
        rag_available = (
            db.query(models.Document)
            .filter(
                models.Document.rag_corpus_id == session.scenario.rag_corpus_id,
                models.Document.status == "ready",
            )
            .count()
            > 0
        )
    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    messages.extend(_convert_history(history_db))

    current_task = _get_task_by_id(session.scenario, session.current_task_id or "")
    if rag_available and current_task and current_task.get("type") == "theory":
        current_question_index = detect_current_theory_question_index(session, db, current_task)
        if current_question_index:
            validation = ensure_theory_validation(
                session=session,
                db=db,
                task=current_task,
                question_index=current_question_index,
            )
            validation_message = format_theory_validation_message(validation)
            if validation_message:
                messages.append({"role": "system", "content": validation_message})

    has_model_messages = any(m.sender == "model" for m in history_db)

    if not has_model_messages:
        messages.append({
            "role": "system",
            "content": (
                "Это САМЫЙ ПЕРВЫЙ ответ модели в этой сессии.\n"
                "Сначала кратко поприветствуй кандидата, "
                "объясни, что это интервью на роль и по сценарию, "
                "а затем сразу задай первый вопрос первого задания.\n"
                "Не пропускай приветствие."
            ),
        })

    try:
        first_resp = lm_client.chat(messages, tools=TOOLS)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls")

    # Fallback: если tool_calls нет, но модель напечатала tool-call текстом
    if not tool_calls:
        content = assistant_msg.get("content") or ""
        inline = _extract_inline_tool_call(content)
        if inline:
            tool_name, args = inline
            assistant_msg, tool_calls = _attach_inline_tool_call(
                assistant_msg,
                tool_name,
                args,
                tool_call_id="inline_toolcall",
            )

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
        score_task_obj = None

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
                score_task_obj = task_obj
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

            theory_flow_error = (
                score_task_obj
                and score_task_obj.get("type") == "theory"
                and (
                    "has not been asked yet" in score_task_error_text
                    or "has not answered question_index" in score_task_error_text
                    or "already exists" in score_task_error_text
                    or "Theory block is not finished yet" in score_task_error_text
                    or "Theory intermediate scores are incomplete" in score_task_error_text
                )
            )

            if theory_flow_error:
                messages.append({
                    "role": "system",
                    "content": (
                        "Предыдущий score_task для theory был преждевременным или логически неверным.\n"
                        f"Причина: {score_task_error_text}\n"
                        "НЕ повторяй score_task прямо сейчас.\n"
                        "Нужно продолжить интервью естественно:\n"
                        "- если это начало theory-блока, начни с приветствия и задай Вопрос 1/N;\n"
                        "- если текущий вопрос уже задан, но кандидат ещё не ответил — задай или повтори текущий вопрос;\n"
                        "- если промежуточная оценка по вопросу уже сохранена — переходи к следующему неотвеченному вопросу.\n"
                        "Не показывай пользователю техническую ошибку и не пиши финальный итог."
                    )
                })
            else:
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
                    final_msg, tool_calls = _attach_inline_tool_call(
                        final_msg,
                        tool_name,
                        args,
                        tool_call_id="inline_toolcall_retry",
                    )

            messages.append(final_msg)
            continue

        allow_tools = _should_allow_final_theory_score_tool(
            session,
            db,
            last_score_result.get("task_id") if isinstance(last_score_result, dict) else None,
            last_score_result if isinstance(last_score_result, dict) else None,
        )

        if allow_tools:
            messages.append({
                "role": "system",
                "content": (
                    "Все промежуточные оценки theory-блока уже сохранены.\n"
                    "Следующим шагом разрешено вызвать финальный score_task с is_final=true.\n"
                    "Не печатай tool-call текстом."
                ),
            })
            final_resp = lm_client.chat(messages, tools=TOOLS)
        else:
            final_resp = lm_client.chat(messages, tools=None)

        final_msg = final_resp["choices"][0]["message"]
        tool_calls = final_msg.get("tool_calls")

        if allow_tools and not tool_calls:
            content = final_msg.get("content") or ""
            inline = _extract_inline_tool_call(content)
            if inline:
                tool_name, args = inline
                final_msg, tool_calls = _attach_inline_tool_call(
                    final_msg,
                    tool_name,
                    args,
                    tool_call_id="inline_toolcall_after_theory_intermediate",
                )

        # если внезапно опять tool_calls — цикл продолжит и обработает
        if not tool_calls:
            break

    # Если модель молчит после score_task — подставляем fallback feedback
    if (not final_msg.get("content")) and last_score_result:
        final_msg["content"] = _score_feedback(last_score_result)

    for tm in tool_results_db:
        db.add(tm)
    final_text = final_msg.get("content") or ""

    # Если модель после score_task напечатала raw tool-dump вместо нормального текста,
    # не сохраняем этот мусор в чат.
    if _looks_like_tool_dump(final_text):
        if isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
            # Для промежуточного theory score_task _score_feedback вернёт "",
            # и тогда ниже мы попросим модель дать нормальный текстовый follow-up.
            final_text = _score_feedback(last_score_result)
        else:
            final_text = _human_tool_error(last_score_result)
        if _looks_like_tool_dump(final_text):
            final_text = ""

    # Если это промежуточный theory score_task и модель не дала нормального текста,
    # просим её ещё раз ответить БЕЗ tools обычным человеческим сообщением.
    if isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
        if not final_text:
            task_id_scored = last_score_result.get("task_id")
            task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

            if task_obj and task_obj.get("type") == "theory" and not bool(last_score_result.get("is_final")):
                messages.append({
                    "role": "system",
                    "content": (
                        "Промежуточная оценка по theory уже успешно сохранена.\n"
                        "Теперь НЕ вызывай tools.\n"
                        "Сделай следующий обычный шаг интервью:\n"
                        "- если есть следующий вопрос, задай его в формате 'Вопрос i/N: ...';\n"
                        "- если это был последний вопрос и финальная оценка уже сохранена, дай итог по теории.\n"
                        "Не печатай технические tool-вызовы."
                    ),
                })

                retry_after_score = lm_client.chat(messages, tools=None)
                retry_msg = retry_after_score["choices"][0]["message"]
                retry_text = retry_msg.get("content") or ""

                if not _looks_like_tool_dump(retry_text):
                    final_msg = retry_msg
                    final_text = retry_text

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





