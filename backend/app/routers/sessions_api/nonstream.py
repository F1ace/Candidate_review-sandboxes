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
from .theory_retry import (build_theory_comment_retry_message, force_pending_theory_intermediate_score, has_unscored_answer_for_current_theory_question, resolve_current_task_id, score_task_only_tools, is_retryable_theory_score_error,)

def _human_tool_error(result: dict) -> str:
    err = ""
    if isinstance(result, dict):
        err = result.get("error") or ""
    if not err:
        err = "неизвестная ошибка"
    return (
        "Не удалось записать оценку автоматически.\n"
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
    needs_intermediate_score, current_task_id, pending_question_index = has_unscored_answer_for_current_theory_question(
        session,
        db,
    )

    if current_task_id and session.current_task_id != current_task_id:
        session.current_task_id = current_task_id
        db.add(session)
        db.commit()
        db.refresh(session)

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


    request_messages = list(messages)

    if needs_intermediate_score and current_task_id and pending_question_index:
        task_obj = _get_task_by_id(session.scenario, current_task_id)
        theory_max_points = int(task_obj.get("max_points", 10) or 10) if task_obj else 10

        request_messages.append({
            "role": "system",
            "content": (
                f"Кандидат уже ответил на theory-вопрос question_index={pending_question_index}. "
                "Следующим приоритетным действием обычно должен быть вызов score_task для сохранения промежуточной оценки. "
                f"Если делаешь score_task, используй: task_id={current_task_id}, is_final=false, "
                f"question_index={pending_question_index}, points в диапазоне 1..{theory_max_points}, "
                "comment на русском языке, с полными завершёнными предложениями. "
                "Если для корректной оценки или продолжения flow действительно нужен другой инструмент, "
                "ты можешь использовать его, но не вызывай финальный score_task преждевременно."
            ),
        })
    try:
        first_resp = lm_client.chat(request_messages, tools=TOOLS)
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
    if needs_intermediate_score and current_task_id and pending_question_index:
        assistant_msg, tool_calls = force_pending_theory_intermediate_score(
            assistant_msg,
            task_id=current_task_id,
            question_index=pending_question_index,
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
            fname = tc["function"]["name"]

            try:
                args_sc = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args_sc = {}

            task_id_for_db = args_sc.get("task_id")

            result = _dispatch_tool_call(session, tc, db)

            if fname == "score_task":
                last_score_result = result

                score_task_obj = _get_task_by_id(
                    session.scenario,
                    task_id_for_db or current_task_id or ""
                )

            if (
                fname == "score_task"
                and score_task_obj
                and score_task_obj.get("type") == "theory"
                and isinstance(result, dict)
                and result.get("ok") is not True
            ):
                flow_error_text = str(result.get("error") or "").strip()

                is_theory_flow_error_local = (
                    "has not been asked yet" in flow_error_text
                    or "has not answered question_index" in flow_error_text
                    or "already exists" in flow_error_text
                )

                if is_theory_flow_error_local:
                    retry_messages = list(messages)

                    retry_messages.append({
                        "role": "system",
                        "content": (
                            "Предыдущий score_task был вызван в неправильный момент.\n"
                            f"Причина: {flow_error_text}\n\n"
                            "НЕ показывай пользователю эту ошибку.\n"
                            "Вернись в корректный flow теоретического интервью:\n"
                            "- если вопрос ещё не задан корректно — задай его;\n"
                            "- если кандидат не ответил — повтори вопрос;\n"
                            "- если ответ уже есть — продолжай интервью.\n"
                            "Не вызывай score_task прямо сейчас."
                        ),
                    })

                    retry_resp = lm_client.chat(retry_messages, tools=TOOLS)
                    retry_assistant_msg = retry_resp["choices"][0]["message"]
                    retry_tool_calls = retry_assistant_msg.get("tool_calls")

                    # inline fallback
                    if not retry_tool_calls:
                        retry_content = retry_assistant_msg.get("content") or ""
                        inline = _extract_inline_tool_call(retry_content)
                        if inline:
                            tool_name, args = inline
                            retry_assistant_msg, retry_tool_calls = _attach_inline_tool_call(
                                retry_assistant_msg,
                                tool_name,
                                args,
                                tool_call_id="inline_toolcall_retry_theory_flow",
                            )

                    messages.append(retry_assistant_msg)
                    tool_calls = retry_tool_calls

                    # важно — не идти дальше старым путём
                    score_task_failed = False
                    score_task_error_text = ""

                    break

                if score_task_obj:
                    score_task_max_points = int(score_task_obj.get("max_points", 10) or 10)

                if not (isinstance(result, dict) and result.get("ok") is True):
                    score_task_failed = True
                    score_task_error_text = ""
                    if isinstance(result, dict):
                        score_task_error_text = str(result.get("error") or "").strip()

            if (
                fname == "score_task"
                and needs_intermediate_score
                and current_task_id
                and pending_question_index
                and is_retryable_theory_score_error(result)
            ):
                task_obj = _get_task_by_id(session.scenario, current_task_id)

                retry_messages = list(messages)
                retry_messages.append({
                    "role": "system",
                    "content": build_theory_comment_retry_message(
                        task_id=current_task_id,
                        question_index=pending_question_index,
                        error_text=result.get("error") or "unknown error",
                        max_points=task_obj.get("max_points") if task_obj else None,
                    ),
                })

                retry_resp = lm_client.chat(
                    retry_messages,
                    tools=score_task_only_tools(),
                    tool_choice="required",
                )

                retry_assistant_msg = retry_resp["choices"][0]["message"]
                retry_tool_calls = retry_assistant_msg.get("tool_calls")

                if not retry_tool_calls:
                    retry_content = retry_assistant_msg.get("content") or ""
                    retry_inline = _extract_inline_tool_call(retry_content)
                    if retry_inline:
                        tool_name, args = retry_inline
                        retry_assistant_msg, retry_tool_calls = _attach_inline_tool_call(
                            retry_assistant_msg,
                            tool_name,
                            args,
                            tool_call_id="inline_toolcall_retry_theory_comment",
                        )

                retry_assistant_msg, retry_tool_calls = force_pending_theory_intermediate_score(
                    retry_assistant_msg,
                    task_id=current_task_id,
                    question_index=pending_question_index,
                )

                if retry_tool_calls:
                    retry_tc = retry_tool_calls[0]
                    retry_result = _dispatch_tool_call(session, retry_tc, db)
                    result = retry_result
                    tc = retry_tc

                    try:
                        args_sc = json.loads(tc["function"].get("arguments", "{}"))
                    except Exception:
                        args_sc = {}

                    task_id_for_db = args_sc.get("task_id")
                    last_score_result = retry_result

                    if isinstance(retry_result, dict) and retry_result.get("ok") is True:
                        score_task_failed = False
                        score_task_error_text = ""
                    else:
                        score_task_failed = True
                        score_task_error_text = str(retry_result.get("error") or "").strip()

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

                exact_points = int(round(float(result.get("points", 0))))
                theory_max_points = int(task_obj2.get("max_points", 10) or 10)
                exact_comment = (result.get("comment") or "").strip()

                aggregated_comments = aggregated.get("comments") or []
                aggregated_comments_text = "\n".join(
                    f"{idx + 1}) {item}"
                    for idx, item in enumerate(aggregated_comments)
                    if str(item).strip()

                
)
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Финальный score_task по теоретическому блоку уже успешно выполнен. "
                            "Сейчас нужно написать ИТОГОВОЕ сообщение кандидату обычным текстом, без tool-call.\n\n"
                            "Структура ответа должна быть строго такой:\n"
                            "1) Короткая фраза о завершении теоретического этапа.\n"
                            "2) Блок с оценкой.\n"
                            "3) Блок с комментарием по содержанию ответов кандидата.\n"
                            "4) Блок с зонами роста.\n"
                            "5) Короткий блок о том, что дальше интервью продолжается в практической части.\n\n"
                            "Критически важно:\n"
                            f"- Используй ТОЧНО итоговую оценку: {exact_points}/{theory_max_points}\n"
                            "- Не придумывай другой балл.\n"
                            "- Не пересчитывай оценку заново.\n"
                            "- Не печатай JSON.\n"
                            "- Не печатай технический текст.\n"
                            "- Не вызывай tools.\n"
                            "- Пиши по-русски.\n"
                            "- Формулируй зоны роста ИНДИВИДУАЛЬНО по ответам кандидата, а не статично.\n"
                            "- Итоговый разбор должен учитывать ВСЕ вопросы теоретического блока, а не только последний вопрос.\n"
                            "- Если финальный comment уже слишком узкий, используй промежуточные comments как основной источник для полного summary.\n"
                            "- В блоке 'Зоны роста' в первую очередь перечисляй КОНКРЕТНЫЕ критичные ошибки и спорные утверждения кандидата из промежуточных comments.\n"
                            "- Если в промежуточных comments есть содержимое после маркера 'ошибка/сомнение:', переформулируй именно его в понятные пункты для кандидата.\n"
                            "- Общие рекомендации допустимы только как дополнение, но не должны заменять описание фактических ошибок.\n"
                            "- Не пиши абстрактные советы вроде 'раскрыть глубже' или 'добавить примеры', если в comments уже есть конкретная ошибка, которую можно назвать прямо.\n"
                            "- Каждый пункт в 'Зонах роста' должен быть привязан к реально допущенной ошибке, неточности или пропуску в ответе кандидата.\n\n"
                            f"Финальный комментарий из score_task:\n{exact_comment}\n\n"
                            f"Промежуточные комментарии по вопросам:\n{aggregated_comments_text}\n"
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

        if tool_calls is not None and not tool_messages and not score_task_failed:
            continue
        # приклеиваем tool-ответы в историю
        messages.extend(tool_messages)

        # Если score_task упал — заставляем модель повторить score_task корректно
        if score_task_failed and retries_left > 0:
            if tool_calls and not tool_messages and not score_task_failed:
                continue
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
                if score_task_obj and score_task_obj.get("type") == "theory":
                    messages.append({
                        "role": "system",
                        "content": (
                            "Предыдущий theory score_task был отклонён системой.\n"
                            f"Причина: {score_task_error_text}\n"
                            f"Если нужно повторить промежуточный score_task для той же theory-задачи, "
                            f"points должен быть в диапазоне [1, {int(score_task_max_points or 10)}].\n"
                            "comment обязателен, должен быть непустым, достаточно подробным и на русском языке.\n"
                            "comment должен содержать минимум 2 полных предложения и не должен выглядеть оборванным.\n"
                            "Желательный формат comment: 'Верно: ... Не хватает: ... Ошибка/сомнение: ...'\n"
                            "Если проблема была в flow, сначала вернись в правильный ход интервью и не показывай пользователю техническую ошибку.\n"
                            "Не печатай техническое сообщение пользователю."
                        )
                    })
                elif score_task_obj and score_task_obj.get("type") in {"coding", "sql"}:
                    if score_task_obj.get("type") == "sql":
                        template_hint = (
                            "Используй comment строго с разделами:\n"
                            "Корректность: ...\n"
                            "Качество решения: ...\n"
                            "Работа с SQL: ...\n"
                            "Что можно улучшить: ..."
                        )
                    else:
                        template_hint = (
                            "Используй comment строго с разделами:\n"
                            "Корректность: ...\n"
                            "Качество кода: ...\n"
                            "Сложность и эффективность: ...\n"
                            "Что можно улучшить: ..."
                        )

                    messages.append({
                        "role": "system",
                        "content": (
                            "Предыдущий practice score_task был отклонён системой.\n"
                            f"Причина: {score_task_error_text}{max_pts_text}\n"
                            f"Повтори score_task ещё раз. points должен быть в диапазоне [0, {int(score_task_max_points or 10)}].\n"
                            "comment обязателен и должен быть заполнен полностью, без пустых разделов.\n"
                            f"{template_hint}\n"
                            "Не пиши пользователю сообщение об ошибке.\n"
                            "Сейчас нужен только исправленный вызов score_task."
                        )
                    })
                else:
                    messages.append({
                        "role": "system",
                        "content": (
                            "score_task НЕ был принят системой.\n"
                            f"Причина: {score_task_error_text}{max_pts_text}\n"
                            "Повтори score_task ещё раз корректно.\n"
                            "comment должен быть непустым.\n"
                            "Не пиши пользователю сообщение об ошибке."
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
            if current_task_id and pending_question_index:
                final_msg, tool_calls = force_pending_theory_intermediate_score(
                    final_msg,
                    task_id=current_task_id,
                    question_index=pending_question_index,
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
