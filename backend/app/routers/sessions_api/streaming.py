import json
import re
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ... import models
from ...database import SessionLocal
from ...services.lm_client import lm_client
from .dispatch import _dispatch_tool_call, _aggregate_theory_intermediate_scores
from .practice import _score_feedback
from .prompting import (_analyze_candidate_message, _build_system_prompt, _extract_inline_tool_call, _strip_intro, _strip_think,)
from .router import logger
from .state import (_control_state, _conversation_snapshot, _convert_history, _first_practice_task, _get_task_by_id, _theory_is_complete, _theory_summary_text,)
from .tool_call_utils import (attach_inline_tool_call as _attach_inline_tool_call, is_score_task_error as _is_score_task_error, looks_like_tool_dump as _looks_like_tool_dump,)
from .tool_errors import (THEORY_COMMENT_EMPTY, THEORY_COMMENT_TOO_SHORT, THEORY_COMMENT_TRUNCATED,)
from .tools import TOOLS

def _sanitize_streamed_text(
    text: str,
    score_result_payload: dict[str, Any] | None,
) -> str:
    text = _strip_think(text or "").strip()

    if not text:
        return ""

    if _looks_like_tool_dump(text):
        if isinstance(score_result_payload, dict):
            if score_result_payload.get("ok") is True:
                return _score_feedback(score_result_payload) or ""
            return _human_tool_error(score_result_payload)
        return ""

    # Дополнительная защита от LM Studio pseudo-tool-call текста
    if "<|channel|>" in text or "<|message|>" in text or "<|constrain|>" in text:
        if isinstance(score_result_payload, dict):
            if score_result_payload.get("ok") is True:
                return _score_feedback(score_result_payload) or ""
            return _human_tool_error(score_result_payload)
        return ""

    return text


def _human_tool_error(result: dict) -> str:
    err = ""
    if isinstance(result, dict):
        err = result.get("error") or ""
    if not err:
        err = "неизвестная ошибка"
    return (
        "Не удалось записать оценку (score_task не принят системой).\n"
        f"Причина: {err}\n"
        "Попробуйте отправить сообщение ещё раз."
    )

def _is_retryable_theory_score_error(
    result: dict[str, Any] | None,
) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is True:
        return False

    err = (result.get("error") or "").strip()
    return err in {
        THEORY_COMMENT_EMPTY,
        THEORY_COMMENT_TOO_SHORT,
        THEORY_COMMENT_TRUNCATED,
    }

def _score_task_only_tools() -> list[dict[str, Any]]:
    return [
        t for t in TOOLS
        if t.get("function", {}).get("name") == "score_task"
    ]


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes"}:
            return True
        if v in {"false", "0", "no"}:
            return False
    return default


def _coerce_inline_tool_call(
    assistant_msg: dict[str, Any],
    *,
    allow_tools: bool,
    tool_call_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    tool_calls = assistant_msg.get("tool_calls")
    if tool_calls or not allow_tools:
        return assistant_msg, tool_calls

    inline = _extract_inline_tool_call((assistant_msg.get("content") or ""))
    if not inline:
        return assistant_msg, None

    tool_name, args = inline
    assistant_msg, tool_calls = _attach_inline_tool_call(
        assistant_msg,
        tool_name,
        args,
        tool_call_id=tool_call_id,
    )
    return assistant_msg, tool_calls

def _force_pending_theory_intermediate_score(
    assistant_msg: dict[str, Any],
    *,
    task_id: str,
    question_index: int,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """
    Если сейчас обязателен промежуточный theory score_task,
    жёстко нормализуем первый tool_call:
    - is_final = false
    - question_index = pending question
    - task_id = текущая theory-задача
    """
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

def _force_final_theory_score(
    assistant_msg: dict[str, Any],
    *,
    task_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    """
    Если сейчас нужен финальный theory score_task,
    жёстко нормализуем первый tool_call:
    - is_final = true
    - question_index = None
    - task_id = текущая theory-задача
    """
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

def _build_theory_question_message(
    session: models.Session,
    task_id: str,
    question_index: int,
) -> str | None:
    task_obj = _get_task_by_id(session.scenario, task_id)
    if not task_obj or task_obj.get("type") != "theory":
        return None

    questions = task_obj.get("questions") or []
    total = len(questions)

    if question_index < 1 or question_index > total:
        return None

    q = questions[question_index - 1]

    if isinstance(q, dict):
        question_text = (
            q.get("text")
            or q.get("question")
            or q.get("prompt")
            or ""
        ).strip()
    else:
        question_text = str(q).strip()

    if not question_text:
        return None

    return f"**Вопрос {question_index}/{total}:** {question_text}"


def _has_unscored_answer_for_current_theory_question(
    session: models.Session,
    db,
) -> tuple[bool, str | None, int | None]:
    current_task_id = _resolve_current_task_id(session, db)
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
        .order_by(models.Message.created_at)
        .all()
    )

    current_question_index = None
    for m in reversed(history):
        if m.sender != "model":
            continue
        txt = (m.text or "").strip()
        match = re.search(r"(?im)вопрос\s+(\d+)\s*/\s*(\d+)", txt)
        if match:
            current_question_index = int(match.group(1))
            break

    if not current_question_index:
        return False, current_task_id, None

    last_candidate = None
    for m in reversed(history):
        if m.sender == "candidate" and (m.text or "").strip():
            last_candidate = m
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


def _resolve_current_task_id(session: models.Session, db) -> str | None:
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


def _should_allow_final_theory_score_tool(
    session: models.Session,
    db,
    task_id: str | None,
    score_result_payload: dict[str, Any] | None,
) -> bool:
    task_id = task_id or _resolve_current_task_id(session, db)
    if not task_id:
        return False

    if not isinstance(score_result_payload, dict):
        return False

    if score_result_payload.get("ok") is not True:
        return False

    if _as_bool(score_result_payload.get("is_final"), default=False):
        return False

    task_obj = _get_task_by_id(session.scenario, task_id)
    if not task_obj or task_obj.get("type") != "theory":
        return False

    aggregated = _aggregate_theory_intermediate_scores(session, db, task_id)
    missing_questions = aggregated.get("missing_questions") or []
    return len(missing_questions) == 0

def stream_model(session_id: str):
    base_db = SessionLocal()
    session = base_db.get(models.Session, session_id)
    if not session:
        base_db.close()
        raise HTTPException(status_code=404, detail="Session not found")

    history_db = (
        base_db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at)
        .all()
    )

    last_msg = history_db[-1] if history_db else None
    if last_msg and last_msg.sender == "candidate":
        candidate_text = (last_msg.text or "").strip()
        flags = set(_analyze_candidate_message(candidate_text) or [])

        hard_reject_flags = {"code_in_chat", "sql_in_chat"}
        soft_reject_flags = {"empty", "too_short", "placeholder", "offtopic", "meaningless"}

        has_hard_reject = bool(flags & hard_reject_flags)
        has_soft_reject = bool(flags & soft_reject_flags)

        word_count = len(candidate_text.split())
        long_enough_answer = word_count >= 25 or len(candidate_text) >= 180
        should_reject = has_hard_reject or (has_soft_reject and not long_enough_answer)

        if should_reject:
            if has_hard_reject:
                warn = "Не вставляйте код/SQL в чат. Введите решение в редактор ниже и нажмите Submit."
            elif "too_short" in flags:
                warn = "Ответ слишком короткий. Раскройте мысль чуть подробнее по сути вопроса."
            elif "placeholder" in flags or "meaningless" in flags:
                warn = "Пожалуйста, дайте осмысленный ответ по сути вопроса."
            else:
                warn = "Ответ не принят: дайте содержательный ответ по сути вопроса."

            base_db.add(models.Message(session_id=session_id, sender="model", text=warn))
            base_db.commit()
            base_db.close()

            def reject_stream():
                yield "data: " + json.dumps({"type": "token", "content": warn}, ensure_ascii=False) + "\n\n"
                yield "data: " + json.dumps({"type": "done", "content": warn}, ensure_ascii=False) + "\n\n"

            return StreamingResponse(reject_stream(), media_type="text/event-stream")

    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = (
            base_db.query(models.Document)
            .filter_by(rag_corpus_id=session.scenario.rag_corpus_id)
            .count() > 0
        )

    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)

    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    base_messages.extend(_convert_history(history_db))

    needs_intermediate_score, current_task_id, pending_question_index = _has_unscored_answer_for_current_theory_question(
        session,
        base_db,
    )

    if current_task_id and session.current_task_id != current_task_id:
        session.current_task_id = current_task_id
        base_db.add(session)
        base_db.commit()
        base_db.refresh(session)

    has_model_messages = any(m.sender == "model" for m in history_db)
    if not has_model_messages:
        base_messages.append({
            "role": "system",
            "content": (
                "Это первый ответ модели в сессии. "
                "Сначала кратко поприветствуй кандидата, "
                "затем сразу задай первый вопрос первого задания."
            ),
        })

    try:
        if needs_intermediate_score and current_task_id and pending_question_index:
            request_messages = list(base_messages)
            request_messages.append({
                "role": "system",
                "content": (
                    f"Кандидат только что ответил на theory-вопрос question_index={pending_question_index}. "
                    "Сейчас нужно обязательно вызвать score_task. "
                    "Требования: is_final=false, корректный question_index, task_id текущей theory-задачи, "
                    "points в диапазоне 1..10, непустой comment на 2-3 ПОЛНЫХ законченных предложения по-русски. Не обрывай фразу на полуслове."
                    "Не пиши обычный текст."
                ),
            })
            first_resp = lm_client.chat(
                request_messages,
                tools=_score_task_only_tools(),
                tool_choice="required",
            )
        else:
            first_resp = lm_client.chat(base_messages, tools=TOOLS)
    except Exception as exc:
        logger.exception("LM request failed before streaming")
        base_db.close()
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    assistant_msg, tool_calls = _coerce_inline_tool_call(
        assistant_msg,
        allow_tools=True,
        tool_call_id="inline_toolcall_initial",
    )

    if needs_intermediate_score and current_task_id and pending_question_index:
        assistant_msg, tool_calls = _force_pending_theory_intermediate_score(
            assistant_msg,
            task_id=current_task_id,
            question_index=pending_question_index,
        )

    stream_messages = list(base_messages)
    tool_results_payload: list[dict[str, Any]] = []
    score_result_payload: dict[str, Any] | None = None
    final_assistant_msg = assistant_msg

    current_assistant_msg = assistant_msg
    current_tool_calls = tool_calls
    max_rounds = 2

    if needs_intermediate_score and current_task_id and pending_question_index:
        current_assistant_msg, current_tool_calls = _force_pending_theory_intermediate_score(
            current_assistant_msg,
            task_id=current_task_id,
            question_index=pending_question_index,
        )

    for _ in range(max_rounds):
        if not current_tool_calls:
            final_assistant_msg = current_assistant_msg
            break

        stream_messages.append(current_assistant_msg)
        last_score_task_id = None

        for tc in current_tool_calls:
            fname = (tc.get("function") or {}).get("name") or ""
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except Exception:
                args = {}

            task_id_for_db = args.get("task_id")

            try:
                result = _dispatch_tool_call(session, tc, base_db)
            except Exception as e:
                logger.exception("Tool failed: %s", fname)
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            # Точечный retry для theory intermediate score_task:
            # если comment слишком короткий/пустой, даём модели один шанс
            # переслать корректный score_task без обычного текста.
            if (
                fname == "score_task"
                and needs_intermediate_score
                and current_task_id
                and pending_question_index
                and _is_retryable_theory_score_error(result)
            ):
                retry_messages = list(stream_messages)
                retry_messages.append({
                    "role": "system",
                    "content": (
                        f"Предыдущий промежуточный score_task для theory-вопроса "
                        f"question_index={pending_question_index} был отклонён backend.\n"
                        f"Причина: {result.get('error') or 'unknown error'}\n\n"
                        f"Сейчас нужно НЕМЕДЛЕННО повторить только tool score_task.\n"
                        f"Исправь только поле comment.\n\n"
                        f"Не меняй:\n"
                        f"- task_id={current_task_id}\n"
                        f"- question_index={pending_question_index}\n"
                        f"- is_final=false\n"
                        f"- points должны остаться корректными для этого ответа\n\n"
                        f"Требования к comment:\n"
                        f"- 1-3 законченных предложения\n"
                        f"- не короче нормального содержательного комментария\n"
                        f"- без обрыва на двоеточии, тире, запятой или точке с запятой\n"
                        f"- кратко объясни, почему выставлены эти баллы\n\n"
                        f"Нельзя:\n"
                        f"- писать обычный текст кандидату\n"
                        f"- задавать следующий вопрос\n"
                        f"- вызывать другой инструмент\n\n"
                        f"Верни только один tool call score_task."
                    ),
                })

                retry_resp = lm_client.chat(
                    retry_messages,
                    tools=_score_task_only_tools(),
                    tool_choice="required",
                )
                retry_assistant_msg = retry_resp["choices"][0]["message"]
                retry_assistant_msg, retry_tool_calls = _coerce_inline_tool_call(
                    retry_assistant_msg,
                    allow_tools=True,
                    tool_call_id="inline_toolcall_retry_theory_comment",
                )

                retry_assistant_msg, retry_tool_calls = _force_pending_theory_intermediate_score(
                    retry_assistant_msg,
                    task_id=current_task_id,
                    question_index=pending_question_index,
                )

                if retry_tool_calls:
                    retry_tc = retry_tool_calls[0]
                    try:
                        retry_result = _dispatch_tool_call(session, retry_tc, base_db)
                    except Exception as e:
                        logger.exception("Retry tool failed: %s", fname)
                        retry_result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                    # заменяем исходный неуспешный результат результатом retry
                    result = retry_result
                    tc = retry_tc

            if fname == "score_task":
                score_result_payload = result
                last_score_task_id = task_id_for_db or result.get("task_id")

            tc_id = tc.get("id") or f"{fname}_call"
            stream_messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": json.dumps(result, ensure_ascii=False),
            })

            tool_results_payload.append({
                "name": fname,
                "result": result,
                "sender": "tool",
                "text": f"{fname} -> {result}",
                "task_id": task_id_for_db,
            })

        if isinstance(score_result_payload, dict) and score_result_payload.get("ok") is not True:
            final_assistant_msg = {
                "role": "assistant",
                "content": _human_tool_error(score_result_payload),
            }
            current_tool_calls = None
            break

        if (
            isinstance(score_result_payload, dict)
            and score_result_payload.get("ok") is True
            and not _as_bool(score_result_payload.get("is_final"), default=False)
            and last_score_task_id
        ):
            task_obj = _get_task_by_id(session.scenario, last_score_task_id)
            if task_obj and task_obj.get("type") == "theory":
                current_qidx = score_result_payload.get("question_index")
                questions = task_obj.get("questions") or []
                total_questions = len(questions)

                if isinstance(current_qidx, int) and current_qidx < total_questions:
                    next_question_text = _build_theory_question_message(
                        session,
                        last_score_task_id,
                        current_qidx + 1,
                    )
                    final_assistant_msg = {
                        "role": "assistant",
                        "content": next_question_text or "",
                    }
                    current_tool_calls = None
                    break

                if _should_allow_final_theory_score_tool(
                    session,
                    base_db,
                    last_score_task_id,
                    score_result_payload,
                ):
                    final_score_messages = list(stream_messages)
                    final_score_messages.append({
                        "role": "system",
                        "content": (
                            "Все промежуточные оценки theory-блока уже сохранены. "
                            "Сейчас нужно вызвать только ФИНАЛЬНЫЙ score_task. "
                            "Обязательно: is_final=true, question_index=null, task_id текущей theory-задачи. "
                            "Нельзя вызывать промежуточный score_task. "
                            "Нельзя писать обычный текст."
                        ),
                    })

                    final_score_resp = lm_client.chat(
                        final_score_messages,
                        tools=_score_task_only_tools(),
                        tool_choice="required",
                    )
                    current_assistant_msg = final_score_resp["choices"][0]["message"]
                    current_assistant_msg, current_tool_calls = _coerce_inline_tool_call(
                        current_assistant_msg,
                        allow_tools=True,
                        tool_call_id="inline_toolcall_final_theory",
                    )
                    current_assistant_msg, current_tool_calls = _force_final_theory_score(
                        current_assistant_msg,
                        task_id=last_score_task_id,
                    )
                    continue

        followup_messages = list(stream_messages)

        is_final_score_ok = bool(
            isinstance(score_result_payload, dict)
            and score_result_payload.get("ok") is True
            and _as_bool(score_result_payload.get("is_final"), default=False)
        )

        if is_final_score_ok:
            exact_points = score_result_payload.get("points")
            exact_comment = (score_result_payload.get("comment") or "").strip()
            aggregated = score_result_payload.get("aggregated") or {}
            aggregated_comments = aggregated.get("comments") or []

            aggregated_comments_text = "\n".join(
                f"- {str(item).strip()}"
                for item in aggregated_comments
                if str(item).strip()
            )

            followup_messages.append({
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
                    f"- Используй ТОЧНО итоговую оценку: {exact_points}/10\n"
                    "- Не придумывай другой балл.\n"
                    "- Не печатай JSON.\n"
                    "- Не печатай технический текст.\n"
                    "- Не вызывай tools.\n"
                    "- Пиши по-русски.\n"
                    "- Формулируй зоны роста ИНДИВИДУАЛЬНО по ответам кандидата, а не статично.\n\n"
                    f"Финальный комментарий из score_task:\n{exact_comment}\n\n"
                    f"Промежуточные комментарии по вопросам:\n{aggregated_comments_text}"
                ),
            })
        else:
            followup_messages.append({
                "role": "system",
                "content": (
                    "Сейчас нужен обычный человеческий ответ интервьюера без tool-call и без технического текста."
                ),
            })

        followup_resp = lm_client.chat(followup_messages, tools=None)
        final_assistant_msg = followup_resp["choices"][0]["message"]
        current_tool_calls = None
        break

    post_tools_assistant_msg = final_assistant_msg or {"role": "assistant", "content": ""}
    base_db.close()

    def event_stream():
        local_db = SessionLocal()
        try:
            local_session = local_db.get(models.Session, session_id)
            if not local_session:
                yield "data: " + json.dumps(
                    {"type": "error", "detail": "Session not found"},
                    ensure_ascii=False,
                ) + "\n\n"
                return

            history_local = (
                local_db.query(models.Message)
                .filter_by(session_id=session_id)
                .order_by(models.Message.created_at)
                .all()
            )
            control_state = _control_state(local_session, history_local)

            # 1. Сохраняем tool-результаты в messages
            for payload in tool_results_payload:
                local_db.add(models.Message(
                    session_id=session_id,
                    sender=payload["sender"],
                    text=payload["text"],
                    task_id=payload.get("task_id"),
                ))

            # 2. Берём уже готовый assistant message после tool-этапа
            raw_final_text = _strip_think((post_tools_assistant_msg or {}).get("content") or "").strip()

            final_text = _sanitize_streamed_text(raw_final_text, score_result_payload).strip()

            if not final_text and isinstance(score_result_payload, dict):
                if score_result_payload.get("ok") is True and _as_bool(score_result_payload.get("is_final"), default=False):
                    final_text = (_score_feedback(score_result_payload) or "").strip()
                elif score_result_payload.get("ok") is not True:
                    final_text = (_human_tool_error(score_result_payload) or "").strip()

            # 5. Если после всего текста нет — не сохраняем пустое model-сообщение
            if final_text:
                trimmed = _strip_intro(final_text, control_state.get("intro_done", False)).strip()
                if trimmed:
                    local_db.add(models.Message(
                        session_id=session_id,
                        sender="model",
                        text=trimmed,
                    ))
                    final_text = trimmed

            local_db.commit()

            yield "data: " + json.dumps(
                {"type": "done", "content": final_text},
                ensure_ascii=False,
            ) + "\n\n"

        except Exception as exc:
            logger.exception("LM streaming failed")
            try:
                local_db.add(models.Message(
                    session_id=session_id,
                    sender="system",
                    text=f"Ошибка сервиса LM Studio: {exc}",
                ))
                local_db.commit()
            except Exception:
                logger.exception("Failed to persist streaming error")
            yield "data: " + json.dumps(
                {"type": "error", "detail": str(exc)},
                ensure_ascii=False,
            ) + "\n\n"
        finally:
            local_db.close()
    return StreamingResponse(event_stream(), media_type="text/event-stream")