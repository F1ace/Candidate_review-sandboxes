import json
import re
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ... import models
from ...database import SessionLocal
from ...services.lm_client import lm_client
from .dispatch import (
    _aggregate_theory_intermediate_scores,
    _dispatch_tool_call,
    _resolve_score_task_is_final,
)
from .practice import _score_feedback
from .prompting import (
    _analyze_candidate_message,
    _build_system_prompt,
    _ensure_first_model_opening,
    _extract_inline_tool_call,
    _normalize_lm_messages,
    _strip_intro,
    _strip_think,
)
from .router import logger
from .state import (_control_state, _conversation_snapshot, _convert_history, _get_task_by_id, _theory_is_complete,)
from .tool_call_utils import (
    attach_inline_tool_call as _attach_inline_tool_call,
    is_score_task_error as _is_score_task_error,
    looks_like_tool_dump as _looks_like_tool_dump,
    strip_trailing_tool_dump as _strip_trailing_tool_dump,
)
from .tools import theory_tools, coding_tools, sql_tools, rag_search_only_tools
from .theory_retry import (build_theory_comment_retry_message, build_final_theory_comment_retry_message, force_final_theory_score, force_pending_theory_intermediate_score, has_unscored_answer_for_current_theory_question, is_retryable_final_theory_score_error, is_retryable_theory_score_error, resolve_current_task_id, score_task_only_tools,)
from .theory_contracts import (build_theory_final_message_contract, build_theory_final_message_prompt, build_theory_final_message_repair_prompt, finalize_theory_final_message, sanitize_theory_final_message, theory_final_message_has_wrong_score, theory_final_message_too_generic,)

def _sanitize_streamed_text(
    text: str,
    score_result_payload: dict[str, Any] | None,
    task_type: str | None = None,
) -> str:
    text = _strip_think(text or "").strip()

    if not text:
        return ""

    if _looks_like_tool_dump(text):
        cleaned = _strip_trailing_tool_dump(text)

        if cleaned:
            return cleaned

        if isinstance(score_result_payload, dict):
            if score_result_payload.get("ok") is True:
                if task_type == "theory" and _as_bool(score_result_payload.get("is_final"), default=False):
                    return ""
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

    lowered = text.lower()
    if "to=functions." in lowered or "to=score_task" in lowered or "to=run_code" in lowered or "to=run_sql" in lowered:
        if isinstance(score_result_payload, dict):
            if score_result_payload.get("ok") is True:
                if task_type == "theory" and _as_bool(score_result_payload.get("is_final"), default=False):
                    return ""
                return _score_feedback(score_result_payload) or ""
            return _human_tool_error(score_result_payload)
        return ""

    return text


def _human_tool_error(result: dict) -> str:
    return (
        "Не удалось автоматически сформировать итоговую оценку. "
        "Попробуйте отправить сообщение ещё раз."
    )

def _is_theory_rag_validation_error(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    return (result.get("error_code") or "") == "theory_rag_validation_required"

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


def _tools_for_current_task(current_task: dict | None, rag_available: bool):
    task_type = (current_task or {}).get("type")
    if task_type == "theory":
        return theory_tools(rag_available=rag_available)
    if task_type == "coding":
        return coding_tools()
    if task_type == "sql":
        return sql_tools()
    return None


def _chat_with_normalized_messages(messages: list[dict[str, Any]], **kwargs):
    return lm_client.chat(_normalize_lm_messages(messages), **kwargs)

def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        name = (tool.get("function") or {}).get("name")
        if name:
            names.add(str(name))
    return names

def _restrict_inline_tools_for_task(
    allowed_tool_names: set[str] | None,
    task_type: str | None,
) -> set[str]:
    names = set(allowed_tool_names or set())
    if task_type == "theory":
        names &= {"rag_search", "web_search", "score_task"}
    return names

def _coerce_inline_tool_call(
    assistant_msg: dict[str, Any],
    *,
    allow_tools: bool,
    tool_call_id: str,
    allowed_tool_names: set[str] | None = None,
    current_task_type: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    tool_calls = assistant_msg.get("tool_calls")
    if tool_calls or not allow_tools:
        return assistant_msg, tool_calls

    safe_allowed = _restrict_inline_tools_for_task(
        allowed_tool_names,
        current_task_type,
    )

    inline = _extract_inline_tool_call(
        (assistant_msg.get("content") or ""),
        allowed_tools=safe_allowed,
    )
    if not inline:
        return assistant_msg, None

    tool_name, args = inline

    if current_task_type == "theory" and tool_name in {"run_code", "run_sql"}:
        return assistant_msg, None

    assistant_msg, tool_calls = _attach_inline_tool_call(
        assistant_msg,
        tool_name,
        args,
        tool_call_id=tool_call_id,
    )
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

def _should_allow_final_theory_score_tool(
    session: models.Session,
    db,
    task_id: str | None,
    score_result_payload: dict[str, Any] | None,
) -> bool:
    task_id = task_id or resolve_current_task_id(session, db)
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

_SCORE_10_RE = re.compile(r"(\d+)\s*/\s*10|\b(\d+)\s+из\s+10\b", re.IGNORECASE)

def _extract_score_mentions_10(text: str) -> list[int]:
    if not text:
        return []
    scores: list[int] = []
    for m in _SCORE_10_RE.finditer(text):
        val = m.group(1) or m.group(2)
        if val is None:
            continue
        try:
            scores.append(int(val))
        except ValueError:
            continue
    return scores

def _final_theory_summary_has_wrong_score(text: str, expected_points: int) -> bool:
    mentions = _extract_score_mentions_10(text or "")
    if not mentions:
        return True
    return any(x != expected_points for x in mentions)

_ISSUE_BULLET_RE = re.compile(r"(?m)^\s*-\s+\*\*.+?\:\*\*")

_THEORY_QUESTION_RE = re.compile(r"(?im)^\s*\*?\*?\s*вопрос\s+\d+\s*/\s*\d+")


def _looks_like_theory_question_prompt(text: str) -> bool:
    return _THEORY_QUESTION_RE.search(text or "") is not None

def _final_theory_summary_too_generic(text: str, expected_question_count: int) -> bool:
    normalized = (text or "").strip().lower()
    if _looks_like_theory_question_prompt(text):
        return True
    if not normalized:
        return True

    required_sections = [
        "итоги теоретической части",
        "сильные стороны",
        "зоны роста",
        "итоговая оценка",
    ]
    if any(section not in normalized for section in required_sections):
        return True

    issue_blocks = _ISSUE_BULLET_RE.findall(text or "")
    if expected_question_count > 0 and len(issue_blocks) < expected_question_count:
        return True

    if len(issue_blocks) < 2:
        return True

    return False

def _build_final_theory_score_repair_message(expected_points: int, theory_max_points: int) -> str:
    return (
        "Предыдущий итоговый текст по теоретическому блоку получился слишком общим или нарушил структуру.\n"
        "Нужно переписать его в следующем формате:\n"
        "1) Заголовок 'Итоги теоретической части'.\n"
        "2) 1-2 предложения общего вывода.\n"
        "3) Отдельный список замечаний по каждому вопросу в формате '- **Тема:** замечание'.\n"
        "4) Блок 'Сильные стороны'.\n"
        "5) Блок 'Зоны роста'.\n"
        "6) Строка с итоговой оценкой.\n"
        "Не добавляй блок перехода к практической части: он будет показан отдельно системой.\n"
        "В списке замечаний по вопросам запрещены промежуточные числовые оценки текстом.\n"
        "Не используй метки вида 'Вопрос 1', 'Вопрос 2'. Используй краткие названия тем.\n"
        "Используй финальный комментарий score_task как главный источник формулировок для замечаний.\n"
        "Не сокращай конкретные замечания до общих слов.\n"
        f"Используй ТОЧНО эту оценку: {expected_points}/{theory_max_points}.\n"
        "Не вызывай tools."
    )

def stream_model(session_id: str):
    base_db = SessionLocal()
    session = base_db.get(models.Session, session_id)
    if not session:
        base_db.close()
        raise HTTPException(status_code=404, detail="Session not found")

    history_db = (
        base_db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
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
            .filter(
                models.Document.rag_corpus_id == session.scenario.rag_corpus_id,
                models.Document.status == "ready",
            )
            .count() > 0
        )

    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)

    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    base_messages.extend(_convert_history(history_db))

    needs_intermediate_score, current_task_id, pending_question_index = has_unscored_answer_for_current_theory_question(
        session,
        base_db,
    )

    if current_task_id and session.current_task_id != current_task_id:
        session.current_task_id = current_task_id
        base_db.add(session)
        base_db.commit()
        base_db.refresh(session)
    current_task = _get_task_by_id(session.scenario, session.current_task_id or "")
    current_task_type = current_task.get("type") if current_task else None
    tools_for_turn = _tools_for_current_task(current_task, rag_available)

    has_model_messages = any(m.sender == "model" for m in history_db)
    if not has_model_messages:
        base_messages.append({
            "role": "system",
            "content": (
                "Это первый ответ модели в сессии. "
                "Нужно дать одно короткое приветствие, "
                "затем сразу задать первый вопрос первого задания. "
                "Не пересказывай весь сценарий интервью."
            ),
        })

    try:
        if needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory":
            request_messages = list(base_messages)
            task_obj = _get_task_by_id(session.scenario, current_task_id)
            theory_max_points = int(task_obj.get("max_points", 10) or 10) if task_obj else 10

            if rag_available:
                request_messages.append({
                    "role": "system",
                    "content": (
                        f"Кандидат только что ответил на theory-вопрос question_index={pending_question_index}. "
                        "Сначала обязательно вызови rag_search по документам сценария. "
                        f"Используй task_id={current_task_id}, question_index={pending_question_index}. "
                        "Сейчас не вызывай score_task."
                    ),
                })
                first_resp = _chat_with_normalized_messages(
                    request_messages,
                    tools=rag_search_only_tools(),
                    tool_choice="required",
                )
            else:
                request_messages.append({
                    "role": "system",
                    "content": (
                        f"Кандидат только что ответил на theory-вопрос question_index={pending_question_index}. "
                        "RAG недоступен, поэтому сейчас нужно обязательно вызвать score_task. "
                        f"Используй task_id={current_task_id}, is_final=false, question_index={pending_question_index}, "
                        f"points в диапазоне 1..{theory_max_points}, comment на 2-3 полных предложения. "
                        "Даже если ответ полностью неверный, нельзя ставить 0 или отрицательное значение: минимальный балл равен 1."
                    ),
                })
                first_resp = _chat_with_normalized_messages(
                    request_messages,
                    tools=score_task_only_tools(),
                    tool_choice="required",
                )
        else:
            first_resp = _chat_with_normalized_messages(base_messages, tools=tools_for_turn)
    except Exception as exc:
        logger.exception("LM request failed before streaming")
        base_db.close()
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    initial_allowed_tool_names = _tool_names(
        rag_search_only_tools() if (needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory" and rag_available)
        else score_task_only_tools() if (needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory" and not rag_available)
        else tools_for_turn
    )
    assistant_msg, tool_calls = _coerce_inline_tool_call(
        assistant_msg,
        allow_tools=True,
        tool_call_id="inline_toolcall_initial",
        allowed_tool_names=initial_allowed_tool_names,
        current_task_type=current_task_type,
    )
    if needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory" and tool_calls:
        assistant_msg, tool_calls = force_pending_theory_intermediate_score(
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
    max_rounds = 4

    for _ in range(max_rounds):
        live_needs_intermediate_score, live_task_id, live_question_index = has_unscored_answer_for_current_theory_question(
            session,
            base_db,
        )
        if (
            live_needs_intermediate_score
            and live_task_id
            and live_question_index
            and current_task_type == "theory"
        ):
            current_assistant_msg, current_tool_calls = force_pending_theory_intermediate_score(
                current_assistant_msg,
                task_id=live_task_id,
                question_index=live_question_index,
            )

        if not current_tool_calls:
            final_assistant_msg = current_assistant_msg
            break

        stream_messages.append(current_assistant_msg)
        last_score_task_id = None
        last_tool_name = None
        followup_after_rag = False

        for tc in current_tool_calls:
            fname = (tc.get("function") or {}).get("name") or ""
            last_tool_name = fname
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except Exception:
                args = {}

            task_id_for_db = args.get("task_id")
            score_task_obj = _get_task_by_id(session.scenario, task_id_for_db) if task_id_for_db else None

            try:
                result = _dispatch_tool_call(session, tc, base_db)
            except Exception as e:
                logger.exception("Tool failed: %s", fname)
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                if (
                    fname in {"run_code", "run_sql"}
                    and current_task_type == "theory"
                    and isinstance(result, dict)
                    and result.get("ok") is False
                ):
                    repair_messages = list(stream_messages)

                    if rag_available and current_task_id and pending_question_index:
                        repair_messages.append({
                            "role": "system",
                            "content": (
                                "Предыдущий вызов был некорректным: в теоретическом блоке нельзя использовать "
                                "run_code и run_sql.\n"
                                "Сейчас нужно вернуть только один корректный tool call rag_search "
                                f"для task_id={current_task_id}, question_index={pending_question_index}.\n"
                                "Не пиши обычный текст. Не вызывай другие tools."
                            ),
                        })

                        repair_resp = _chat_with_normalized_messages(
                            repair_messages,
                            tools=rag_search_only_tools(),
                            tool_choice="required",
                        )
                        repair_msg = repair_resp["choices"][0]["message"]
                        repair_msg, repair_tool_calls = _coerce_inline_tool_call(
                            repair_msg,
                            allow_tools=True,
                            tool_call_id="inline_toolcall_invalid_theory_run_tool_repair_rag",
                            allowed_tool_names=_tool_names(rag_search_only_tools()),
                            current_task_type=current_task_type,
                        )

                        current_assistant_msg = repair_msg
                        current_tool_calls = repair_tool_calls
                        break

                    if current_task_id and pending_question_index:
                        repair_messages.append({
                            "role": "system",
                            "content": (
                                "Предыдущий вызов был некорректным: в теоретическом блоке нельзя использовать "
                                "run_code и run_sql.\n"
                                "Сейчас нужно вернуть только один корректный tool call score_task "
                                f"для task_id={current_task_id}, question_index={pending_question_index}, is_final=false.\n"
                                "Не пиши обычный текст. Не вызывай другие tools."
                            ),
                        })

                        repair_resp = _chat_with_normalized_messages(
                            repair_messages,
                            tools=score_task_only_tools(),
                            tool_choice="required",
                        )
                        repair_msg = repair_resp["choices"][0]["message"]
                        repair_msg, repair_tool_calls = _coerce_inline_tool_call(
                            repair_msg,
                            allow_tools=True,
                            tool_call_id="inline_toolcall_invalid_theory_run_tool_repair_score",
                            allowed_tool_names=_tool_names(score_task_only_tools()),
                            current_task_type=current_task_type,
                        )

                        repair_msg, repair_tool_calls = force_pending_theory_intermediate_score(
                            repair_msg,
                            task_id=current_task_id,
                            question_index=pending_question_index,
                        )

                        current_assistant_msg = repair_msg
                        current_tool_calls = repair_tool_calls
                        break

            if (
                fname == "score_task"
                and needs_intermediate_score
                and current_task_id
                and pending_question_index
                and is_retryable_theory_score_error(result)
            ):
                retry_messages = list(stream_messages)
                task_obj = _get_task_by_id(session.scenario, current_task_id) if current_task_id else None

                retry_messages.append({
                    "role": "system",
                    "content": build_theory_comment_retry_message(
                        task_id=current_task_id,
                        question_index=pending_question_index,
                        error_text=result.get("error") or "unknown error",
                        max_points=task_obj.get("max_points") if task_obj else None,
                    ),
                })

                retry_resp = _chat_with_normalized_messages(
                    retry_messages,
                    tools=score_task_only_tools(),
                    tool_choice="required",
                )
                retry_assistant_msg = retry_resp["choices"][0]["message"]
                retry_assistant_msg, retry_tool_calls = _coerce_inline_tool_call(
                    retry_assistant_msg,
                    allow_tools=True,
                    tool_call_id="inline_toolcall_retry_theory_comment",
                    allowed_tool_names=_tool_names(score_task_only_tools()),
                    current_task_type=current_task_type,
                )

                retry_assistant_msg, retry_tool_calls = force_pending_theory_intermediate_score(
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

            if (
                fname == "score_task"
                and task_id_for_db
                and score_task_obj
                and score_task_obj.get("type") == "theory"
                and _resolve_score_task_is_final(
                    args,
                    task_type=score_task_obj.get("type"),
                    question_index=args.get("question_index"),
                ) is True
                and is_retryable_final_theory_score_error(result)
            ):
                retry_messages = list(stream_messages)

                retry_messages.append({
                    "role": "system",
                    "content": build_final_theory_comment_retry_message(
                        task_id=task_id_for_db,
                        error_text=result.get("error") or "unknown error",
                        max_points=score_task_obj.get("max_points") if score_task_obj else None,
                    ),
                })

                retry_resp = _chat_with_normalized_messages(
                    retry_messages,
                    tools=score_task_only_tools(),
                    tool_choice="required",
                )
                retry_assistant_msg = retry_resp["choices"][0]["message"]
                retry_assistant_msg, retry_tool_calls = _coerce_inline_tool_call(
                    retry_assistant_msg,
                    allow_tools=True,
                    tool_call_id="inline_toolcall_retry_final_theory_comment",
                    allowed_tool_names=_tool_names(score_task_only_tools()),
                    current_task_type=current_task_type,
                )

                retry_assistant_msg, retry_tool_calls = force_final_theory_score(
                    retry_assistant_msg,
                    task_id=task_id_for_db,
                )

                if retry_tool_calls:
                    retry_tc = retry_tool_calls[0]
                    try:
                        retry_result = _dispatch_tool_call(session, retry_tc, base_db)
                    except Exception as e:
                        logger.exception("Retry tool failed: %s", fname)
                        retry_result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

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
            if (
                fname == "rag_search"
                and current_task_type == "theory"
                and current_task_id
                and pending_question_index
                and isinstance(result, dict)
                and result.get("ok") is True
            ):
                followup_messages = list(stream_messages)
                followup_messages.append({
                    "role": "system",
                    "content": (
                        "Поиск по документам сценария уже выполнен. "
                        "Теперь верни только один tool call score_task для этого же theory-вопроса. "
                        f"Используй task_id={current_task_id}, question_index={pending_question_index}, is_final=false. "
                        f"points должен быть в диапазоне [1, {int((_get_task_by_id(session.scenario, current_task_id) or {}).get('max_points', 10) or 10)}]. "
                        "Даже если ответ полностью неверный, минимальный балл для theory равен 1."
                    ),
                })

                next_resp = _chat_with_normalized_messages(
                    followup_messages,
                    tools=score_task_only_tools(),
                    tool_choice="required",
                )
                next_assistant_msg = next_resp["choices"][0]["message"]
                next_assistant_msg, next_tool_calls = _coerce_inline_tool_call(
                    next_assistant_msg,
                    allow_tools=True,
                    tool_call_id="inline_toolcall_after_rag_search",
                    allowed_tool_names=_tool_names(score_task_only_tools()),
                    current_task_type=current_task_type,
                )

                next_assistant_msg, next_tool_calls = force_pending_theory_intermediate_score(
                    next_assistant_msg,
                    task_id=current_task_id,
                    question_index=pending_question_index,
                )

                current_assistant_msg = next_assistant_msg
                current_tool_calls = next_tool_calls
                followup_after_rag = True
                break

        if followup_after_rag:
            continue

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
                    final_theory_aggregated = _aggregate_theory_intermediate_scores(
                        session,
                        base_db,
                        last_score_task_id,
                    )
                    avg_points_hint = ""
                    if final_theory_aggregated.get("avg_points") is not None:
                        avg_points_hint = (
                            f"- avg_points уже рассчитан системой и равен {int(final_theory_aggregated['avg_points'])}\n"
                            f"- points не должен быть выше {int(final_theory_aggregated['avg_points'])}\n"
                        )
                    final_score_messages = list(stream_messages)
                    final_score_messages.append({
                        "role": "system",
                        "content": (
                            "Все промежуточные оценки theory-блока уже сохранены. "
                            "Сейчас нужно вернуть только один ФИНАЛЬНЫЙ score_task.\n\n"
                            "Обязательные поля:\n"
                            "- is_final=true\n"
                            "- question_index=null\n"
                            "- task_id текущей theory-задачи\n"
                            "- points отдельным числом\n"
                            f"{avg_points_hint}"
                            "- comment: общий качественный итог по всему theory-блоку\n"
                            "- comments: массив комментариев по каждому вопросу в порядке вопросов\n\n"
                            "Требования к comment:\n"
                            "- по-русски\n"
                            "- 3-6 законченных предложений\n"
                            "- кратко резюмирует весь теоретический блок\n"
                            "- описывает сильные стороны кандидата, ключевые ошибки и зоны роста\n"
                            "- без числовой оценки текстом\n\n"
                            "Требования к comments:\n"
                            "- один элемент на каждый вопрос текущей theory-задачи, строго по порядку\n"
                            "- каждый элемент конкретно описывает ответ кандидата по соответствующему вопросу\n"
                            "- без числовой оценки текстом\n"
                            "- не объединяй несколько вопросов в один пункт\n"
                            "- не заменяй конкретику общими фразами\n\n"
                            "Верни только один tool call score_task."
                        ),
                    })

                    final_score_resp = _chat_with_normalized_messages(
                        final_score_messages,
                        tools=score_task_only_tools(),
                        tool_choice="required",
                    )
                    current_assistant_msg = final_score_resp["choices"][0]["message"]
                    current_assistant_msg, current_tool_calls = _coerce_inline_tool_call(
                        current_assistant_msg,
                        allow_tools=True,
                        tool_call_id="inline_toolcall_final_theory",
                        allowed_tool_names=_tool_names(score_task_only_tools()),
                        current_task_type=current_task_type,
                    )
                    current_assistant_msg, current_tool_calls = force_final_theory_score(
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

        final_task_id = None
        if isinstance(score_result_payload, dict):
            final_task_id = (
                last_score_task_id
                or score_result_payload.get("task_id")
                or current_task_id
            )
        else:
            final_task_id = last_score_task_id or current_task_id

        final_task_obj = _get_task_by_id(session.scenario, final_task_id) if final_task_id else None

        theory_contract = None
        if is_final_score_ok and final_task_obj and final_task_obj.get("type") == "theory":
            theory_contract = build_theory_final_message_contract(
                final_task_obj,
                score_result_payload,
            )

        if is_final_score_ok:
            if theory_contract:
                followup_messages.append({
                    "role": "system",
                    "content": build_theory_final_message_prompt(theory_contract),
                })
            else:
                exact_points = int(round(float(score_result_payload.get("points", 0) or 0)))
                followup_messages.append({
                    "role": "system",
                    "content": (
                        "Финальный score_task по теоретическому блоку уже успешно выполнен. "
                        "Сейчас нужно написать короткое итоговое сообщение кандидату обычным текстом, без tool-call.\n"
                        f"Используй точную итоговую оценку: {exact_points}/10.\n"
                        "Скажи, что теоретический блок завершён, и кратко перечисли сильные стороны и зоны роста."
                    ),
                })
        else:
            followup_messages.append({
                "role": "system",
                "content": (
                    "Сейчас нужен обычный человеческий ответ интервьюера без tool-call и без технического текста."
                ),
            })

        followup_resp = _chat_with_normalized_messages(followup_messages, tools=None)
        final_assistant_msg = followup_resp["choices"][0]["message"]
        current_tool_calls = None
        final_text = final_assistant_msg.get("content") or ""
        final_text = _strip_trailing_tool_dump(final_text)

        if theory_contract:
            final_text = sanitize_theory_final_message(final_text, theory_contract)
            for _ in range(2):
                needs_score_repair = theory_final_message_has_wrong_score(final_text, theory_contract)
                needs_quality_repair = theory_final_message_too_generic(final_text, theory_contract)
                if (
                    not needs_score_repair
                    and not needs_quality_repair
                    and not _looks_like_tool_dump(final_text)
                ):
                    break

                repair_messages = list(followup_messages)
                repair_messages.append({
                    "role": "assistant",
                    "content": final_text,
                })
                repair_messages.append({
                    "role": "system",
                    "content": build_theory_final_message_repair_prompt(
                        theory_contract,
                        previous_text=final_text,
                        score_issue=needs_score_repair,
                        quality_issue=needs_quality_repair,
                    ),
                })

                repair_resp = _chat_with_normalized_messages(repair_messages, tools=None)
                repair_msg = repair_resp["choices"][0]["message"]
                repair_text = repair_msg.get("content") or ""
                repair_text = _strip_trailing_tool_dump(repair_text)
                repair_text = sanitize_theory_final_message(repair_text, theory_contract)

                if not _looks_like_tool_dump(repair_text):
                    final_assistant_msg = repair_msg
                    final_text = repair_text
            final_text = finalize_theory_final_message(final_text, theory_contract)
        final_assistant_msg["content"] = final_text
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
                .order_by(models.Message.created_at.asc(), models.Message.id.asc())
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

            task_type = None
            if isinstance(score_result_payload, dict):
                task_id_scored = score_result_payload.get("task_id")
                task_obj_local = _get_task_by_id(local_session.scenario, task_id_scored) if task_id_scored else None
                if task_obj_local:
                    task_type = task_obj_local.get("type")

            final_text = _sanitize_streamed_text(raw_final_text, score_result_payload, task_type=task_type).strip()
            if isinstance(score_result_payload, dict) and score_result_payload.get("ok") is True:
                task_id_scored = score_result_payload.get("task_id")
                task_obj_local = _get_task_by_id(local_session.scenario, task_id_scored) if task_id_scored else None
                if task_obj_local and task_obj_local.get("type") == "theory" and _as_bool(score_result_payload.get("is_final"), default=False):
                    theory_contract_local = build_theory_final_message_contract(task_obj_local, score_result_payload)
                    if theory_contract_local:
                        final_text = finalize_theory_final_message(final_text, theory_contract_local)

            if not final_text and isinstance(score_result_payload, dict):
                if score_result_payload.get("ok") is True and _as_bool(score_result_payload.get("is_final"), default=False):
                    if not (task_type == "theory"):
                        final_text = (_score_feedback(score_result_payload) or "").strip()
                elif score_result_payload.get("ok") is not True:
                    final_text = (_human_tool_error(score_result_payload) or "").strip()
                elif score_result_payload.get("ok") is not True:
                    final_text = (_human_tool_error(score_result_payload) or "").strip()

            # 5. Если после всего текста нет — не сохраняем пустое model-сообщение
            if final_text:
                final_text = final_text.strip()
                if not control_state.get("intro_done", False):
                    final_text = _ensure_first_model_opening(final_text, local_session)
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
