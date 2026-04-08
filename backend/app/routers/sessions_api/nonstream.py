import json
from typing import Any
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ... import models
from ...services.lm_client import lm_client
from .dispatch import (
    _aggregate_theory_intermediate_scores,
    _dispatch_tool_call,
    _resolve_score_task_is_final,
)
from .practice import _score_feedback
from .prompting import (
    _build_system_prompt,
    _ensure_first_model_greeting,
    _extract_inline_tool_call,
    _normalize_lm_messages,
    _strip_intro,
)
from .state import (_conversation_snapshot, _convert_history, _get_task_by_id, _theory_is_complete,)
from .tool_call_utils import (
    attach_inline_tool_call as _attach_inline_tool_call,
    is_score_task_error as _is_score_task_error,
    looks_like_tool_dump as _looks_like_tool_dump,
    strip_trailing_tool_dump as _strip_trailing_tool_dump,
)
from .tools import theory_tools, coding_tools, sql_tools, rag_search_only_tools
from .theory_retry import (build_theory_comment_retry_message, build_final_theory_comment_retry_message, force_pending_theory_intermediate_score, force_final_theory_score, has_unscored_answer_for_current_theory_question, resolve_current_task_id, score_task_only_tools, is_retryable_final_theory_score_error, is_retryable_theory_score_error,)
from .theory_contracts import (build_theory_final_message_contract, build_theory_final_message_prompt, build_theory_final_message_repair_prompt, sanitize_theory_final_message, theory_final_message_has_wrong_score, theory_final_message_too_generic,)

def _human_tool_error(result: dict) -> str:
    return (
        "Не удалось автоматически сформировать итоговую оценку. "
        "Попробуйте отправить сообщение ещё раз."
    )

def _chat_with_normalized_messages(messages: list[dict[str, Any]], **kwargs):
    return lm_client.chat(_normalize_lm_messages(messages), **kwargs)


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
        "После уже успешного финального theory score_task нельзя задавать новые вопросы кандидату.\n"
        "Если предыдущий ответ был в форме 'Вопрос i/N: ...', полностью перепиши его в итоговый summary.\n"
        "Не используй шаблон вида '1) Блок с оценкой / 2) Блок с комментарием / 3) Блок с зонами роста / Что дальше'.\n"
        "Не добавляй JSON, словарь или tool payload с полями ok/task_id/points/comment.\n"
        "Не вызывай tools."
    )

def _tools_for_current_task(current_task: dict | None, rag_available: bool):
    task_type = (current_task or {}).get("type")
    if task_type == "theory":
        return theory_tools(rag_available=rag_available)
    if task_type == "coding":
        return coding_tools()
    if task_type == "sql":
        return sql_tools()
    return None

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
    allowed_tool_names: set[str] | None,
    current_task_type: str | None,
    tool_call_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    tool_calls = assistant_msg.get("tool_calls")
    if tool_calls:
        return assistant_msg, tool_calls

    safe_allowed = _restrict_inline_tools_for_task(
        allowed_tool_names,
        current_task_type,
    )

    inline = _extract_inline_tool_call(
        assistant_msg.get("content") or "",
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

def call_model(session_id: str, db: Session):
    """Non-streaming call (fallback)."""
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    history_db = (
        db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
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
    current_task_type = current_task.get("type") if current_task else None
    tools_for_turn = _tools_for_current_task(current_task, rag_available)

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

    if needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory":
        task_obj = _get_task_by_id(session.scenario, current_task_id)
        theory_max_points = int(task_obj.get("max_points", 10) or 10) if task_obj else 10

        if rag_available:
            request_messages.append({
                "role": "system",
                "content": (
                    f"Кандидат уже ответил на theory-вопрос question_index={pending_question_index}. "
                    "Сначала обязательно вызови rag_search по документам сценария. "
                    f"Используй task_id={current_task_id}, question_index={pending_question_index}. "
                    "После rag_search будет отдельный шаг со score_task. "
                    "Сейчас не вызывай score_task."
                ),
            })
        else:
            request_messages.append({
                "role": "system",
                "content": (
                    f"Кандидат уже ответил на theory-вопрос question_index={pending_question_index}. "
                    "RAG недоступен, поэтому сейчас нужно сразу вызвать промежуточный score_task. "
                    f"Используй task_id={current_task_id}, is_final=false, question_index={pending_question_index}, "
                    f"points в диапазоне 1..{theory_max_points}, comment на русском языке, минимум 2 полных предложения. "
                    "Даже если ответ полностью неверный, нельзя ставить 0 или отрицательное значение: минимальный балл равен 1."
                ),
            })

    try:
        if needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory":
            if rag_available:
                first_resp = _chat_with_normalized_messages(
                    request_messages,
                    tools=rag_search_only_tools(),
                    tool_choice="required",
                )
            else:
                first_resp = _chat_with_normalized_messages(
                    request_messages,
                    tools=score_task_only_tools(),
                    tool_choice="required",
                )
        else:
            first_resp = _chat_with_normalized_messages(request_messages, tools=tools_for_turn)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls")

    # Fallback: если tool_calls нет, но модель напечатала tool-call текстом
    assistant_msg, tool_calls = _coerce_inline_tool_call(
        assistant_msg,
        allowed_tool_names=_tool_names(
            rag_search_only_tools() if (needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory" and rag_available)
            else score_task_only_tools() if (needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory" and not rag_available)
            else tools_for_turn
        ),
        current_task_type=current_task_type,
        tool_call_id="inline_toolcall",
    )
    if needs_intermediate_score and current_task_id and pending_question_index and current_task_type == "theory" and tool_calls:
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
        live_needs_intermediate_score, live_task_id, live_question_index = has_unscored_answer_for_current_theory_question(
            session,
            db,
        )
        if (
            live_needs_intermediate_score
            and live_task_id
            and live_question_index
            and current_task_type == "theory"
        ):
            final_msg, tool_calls = force_pending_theory_intermediate_score(
                final_msg,
                task_id=live_task_id,
                question_index=live_question_index,
            )

        tool_messages = []
        last_tool_name = None
        score_task_failed = False
        score_task_error_text = ""
        score_task_max_points = None
        score_task_obj = None

        for tc in tool_calls:
            fname = tc["function"]["name"]
            last_tool_name = fname

            try:
                args_sc = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args_sc = {}

            task_id_for_db = args_sc.get("task_id")

            result = _dispatch_tool_call(session, tc, db)
            if (
                fname in {"run_code", "run_sql"}
                and current_task_type == "theory"
                and isinstance(result, dict)
                and result.get("ok") is False
            ):
                repair_messages = list(messages)

                if rag_available and current_task_id and pending_question_index:
                    repair_messages.append({
                        "role": "system",
                        "content": (
                            "Предыдущий вызов был некорректным: в теоретическом блоке нельзя использовать "
                            "run_code и run_sql.\n"
                            "Сейчас нужно вернуть только один корректный tool call rag_search "
                            f"для task_id={current_task_id}, question_index={pending_question_index}.\n"
                            "Не пиши обычный текст и не вызывай другие tools."
                        ),
                    })

                    repair_resp = _chat_with_normalized_messages(
                        repair_messages,
                        tools=rag_search_only_tools(),
                        tool_choice="required",
                    )
                    final_msg = repair_resp["choices"][0]["message"]
                    final_msg, tool_calls = _coerce_inline_tool_call(
                        final_msg,
                        allowed_tool_names=_tool_names(rag_search_only_tools()),
                        current_task_type=current_task_type,
                        tool_call_id="inline_toolcall_invalid_theory_run_tool_repair_rag",
                    )
                    messages.append(final_msg)
                    break

                if current_task_id and pending_question_index:
                    repair_messages.append({
                        "role": "system",
                        "content": (
                            "Предыдущий вызов был некорректным: в теоретическом блоке нельзя использовать "
                            "run_code и run_sql.\n"
                            "Сейчас нужно вернуть только один корректный tool call score_task "
                            f"для task_id={current_task_id}, question_index={pending_question_index}, is_final=false.\n"
                            "Не пиши обычный текст и не вызывай другие tools."
                        ),
                    })

                    repair_resp = _chat_with_normalized_messages(
                        repair_messages,
                        tools=score_task_only_tools(),
                        tool_choice="required",
                    )
                    final_msg = repair_resp["choices"][0]["message"]
                    final_msg, tool_calls = _coerce_inline_tool_call(
                        final_msg,
                        allowed_tool_names=_tool_names(score_task_only_tools()),
                        current_task_type=current_task_type,
                        tool_call_id="inline_toolcall_invalid_theory_run_tool_repair_score",
                    )
                    final_msg, tool_calls = force_pending_theory_intermediate_score(
                        final_msg,
                        task_id=current_task_id,
                        question_index=pending_question_index,
                    )
                    messages.append(final_msg)
                    break

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

                    retry_resp = _chat_with_normalized_messages(retry_messages, tools=tools_for_turn)
                    retry_assistant_msg = retry_resp["choices"][0]["message"]
                    retry_tool_calls = retry_assistant_msg.get("tool_calls")

                    retry_assistant_msg, retry_tool_calls = _coerce_inline_tool_call(
                        retry_assistant_msg,
                        allowed_tool_names=_tool_names(tools_for_turn),
                        current_task_type=current_task_type,
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

                retry_resp = _chat_with_normalized_messages(
                    retry_messages,
                    tools=score_task_only_tools(),
                    tool_choice="required",
                )

                retry_assistant_msg = retry_resp["choices"][0]["message"]
                retry_tool_calls = retry_assistant_msg.get("tool_calls")

                retry_assistant_msg, retry_tool_calls = _coerce_inline_tool_call(
                    retry_assistant_msg,
                    allowed_tool_names=_tool_names(score_task_only_tools()),
                    current_task_type=current_task_type,
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

            if (
                fname == "score_task"
                and score_task_obj
                and score_task_obj.get("type") == "theory"
                and _resolve_score_task_is_final(
                    args_sc,
                    task_type=score_task_obj.get("type"),
                    question_index=args_sc.get("question_index"),
                ) is True
                and task_id_for_db
                and is_retryable_final_theory_score_error(result)
            ):
                retry_messages = list(messages)

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
                retry_tool_calls = retry_assistant_msg.get("tool_calls")

                retry_assistant_msg, retry_tool_calls = _coerce_inline_tool_call(
                    retry_assistant_msg,
                    allowed_tool_names=_tool_names(score_task_only_tools()),
                    current_task_type=current_task_type,
                    tool_call_id="inline_toolcall_retry_final_theory_comment",
                )

                retry_assistant_msg, retry_tool_calls = force_final_theory_score(
                    retry_assistant_msg,
                    task_id=task_id_for_db,
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
            theory_contract = None
            if (
                task_obj2
                and task_obj2.get("type") == "theory"
                and isinstance(result, dict)
                and result.get("ok") is True
                and result.get("is_final") is True
                and _theory_is_complete(session)
            ):
                theory_contract = build_theory_final_message_contract(task_obj2, result)
                if theory_contract:
                    messages.append(
                        {
                            "role": "system",
                            "content": build_theory_final_message_prompt(theory_contract),
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
                        "НЕ начинай theory-блок заново.\n"
                        "Нужно продолжить интервью естественно, строго из текущего состояния:\n"
                        "- если текущий theory-вопрос уже задан, но кандидат ещё не ответил — повтори именно текущий вопрос;\n"
                        "- если промежуточная оценка по текущему вопросу уже сохранена — переходи к следующему неотвеченному вопросу;\n"
                        "- если все вопросы уже пройдены, не задавай новые вопросы и не возвращайся к 'Вопрос 1/N'.\n"
                        "Не показывай пользователю техническую ошибку и не пиши финальный итог, если блок ещё не завершён."
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
                    "Даже если ответ кандидата полностью неверный, для theory нельзя ставить 0 или отрицательное значение: минимум равен 1.\n"
                    "comment обязателен, должен быть непустым, достаточно подробным и на русском языке.\n"
                    "comment должен содержать минимум 2 полных предложения и не должен выглядеть оборванным.\n"
                    "Желательный формат comment: 'Верно: ... Не хватает: ... Ошибка/сомнение: ...'\n"
                    "Если проблема была в flow, сначала вернись в правильный ход интервью и не показывай пользователю техническую ошибку.\n"
                            "Если это финальный theory score_task, comment должен быть только качественным итогом по всему блоку.\n"
                            "В финальном theory comment нельзя писать числовую оценку текстом: '7/10', '6 из 10', 'ставлю 7', 'оценка 5'.\n"
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

            retry_tools = tools_for_turn
            retry_tool_choice = None

            if score_task_obj and score_task_obj.get("type") in {"theory", "coding", "sql"}:
                retry_tools = score_task_only_tools()
                retry_tool_choice = "required"

            if retry_tool_choice:
                retry_resp = _chat_with_normalized_messages(
                    messages,
                    tools=retry_tools,
                    tool_choice=retry_tool_choice,
                )
            else:
                retry_resp = _chat_with_normalized_messages(messages, tools=retry_tools)

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

        require_intermediate_score_after_rag = (
            current_task_type == "theory"
            and last_tool_name == "rag_search"
            and current_task_id
            and pending_question_index
        )

        allow_final_theory_score_tool = _should_allow_final_theory_score_tool(
            session,
            db,
            last_score_result.get("task_id") if isinstance(last_score_result, dict) else None,
            last_score_result if isinstance(last_score_result, dict) else None,
        )

        if require_intermediate_score_after_rag:
            messages.append({
                "role": "system",
                "content": (
                    "Поиск по документам сценария уже выполнен. "
                    "Теперь верни только один tool call score_task для этого же theory-вопроса. "
                    f"Используй task_id={current_task_id}, question_index={pending_question_index}, is_final=false. "
                    f"points должен быть в диапазоне [1, {int((_get_task_by_id(session.scenario, current_task_id) or {}).get('max_points', 10) or 10)}]. "
                    "Даже если ответ полностью неверный, минимальный балл для theory равен 1."
                ),
            })
            final_resp = _chat_with_normalized_messages(
                messages,
                tools=score_task_only_tools(),
                tool_choice="required",
            )
        elif allow_final_theory_score_tool:
            final_theory_task_id = (
                last_score_result.get("task_id")
                if isinstance(last_score_result, dict)
                else current_task_id
            )
            final_theory_aggregated = (
                _aggregate_theory_intermediate_scores(session, db, final_theory_task_id)
                if final_theory_task_id
                else None
            )
            avg_points_hint = ""
            if isinstance(final_theory_aggregated, dict) and final_theory_aggregated.get("avg_points") is not None:
                avg_points_hint = (
                    f"- avg_points уже рассчитан системой и равен {int(final_theory_aggregated['avg_points'])}\n"
                    f"- points не должен быть выше {int(final_theory_aggregated['avg_points'])}\n"
                )
            messages.append({
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
            final_resp = _chat_with_normalized_messages(
                messages,
                tools=score_task_only_tools(),
                tool_choice="required",
            )
        else:
            final_resp = _chat_with_normalized_messages(messages, tools=None)

        final_msg = final_resp["choices"][0]["message"]
        tool_calls = final_msg.get("tool_calls")

        if (require_intermediate_score_after_rag or allow_final_theory_score_tool) and not tool_calls:
            content = final_msg.get("content") or ""
            inline = _extract_inline_tool_call(content)
            if inline:
                tool_name, args = inline
                final_msg, tool_calls = _attach_inline_tool_call(
                    final_msg,
                    tool_name,
                    args,
                    tool_call_id="inline_toolcall_after_theory_step",
                )

        if require_intermediate_score_after_rag and tool_calls:
            final_msg, tool_calls = force_pending_theory_intermediate_score(
                final_msg,
                task_id=current_task_id,
                question_index=pending_question_index,
            )

        if not tool_calls:
            break

    # Если модель молчит после score_task, подставляем fallback feedback только для практики.
    if (not final_msg.get("content")) and last_score_result:
        task_id_scored = last_score_result.get("task_id") if isinstance(last_score_result, dict) else None
        task_obj_local = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None
        if not (task_obj_local and task_obj_local.get("type") == "theory" and bool(last_score_result.get("is_final"))):
            final_msg["content"] = _score_feedback(last_score_result)

    for tm in tool_results_db:
        db.add(tm)
    final_text = final_msg.get("content") or ""
    final_text = _strip_trailing_tool_dump(final_text)
    lowered_final = (final_text or "").lower()
    if (
        "<|start|>assistant" in lowered_final
        or "to=functions." in lowered_final
        or "to=score_task" in lowered_final
        or "to=run_code" in lowered_final
        or "to=run_sql" in lowered_final
    ):
        final_text = _strip_trailing_tool_dump(final_text or "")
        if _looks_like_tool_dump(final_text) or not final_text.strip():
            final_text = ""

    if isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
        task_id_scored = last_score_result.get("task_id")
        task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

        if task_obj and task_obj.get("type") == "theory" and bool(last_score_result.get("is_final")):
            theory_contract = build_theory_final_message_contract(task_obj, last_score_result)
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

                    repair_messages = list(messages)
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
                        final_msg = repair_msg
                        final_text = repair_text

    # Если модель после score_task напечатала raw tool-dump вместо нормального текста,
    # не сохраняем этот мусор в чат.
    if _looks_like_tool_dump(final_text):
        final_text = _strip_trailing_tool_dump(final_text)

        if not final_text and isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
            task_id_scored = last_score_result.get("task_id")
            task_obj_local = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

            if not (task_obj_local and task_obj_local.get("type") == "theory" and bool(last_score_result.get("is_final"))):
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

                retry_after_score = _chat_with_normalized_messages(messages, tools=None)
                retry_msg = retry_after_score["choices"][0]["message"]
                retry_text = retry_msg.get("content") or ""

                if not _looks_like_tool_dump(retry_text):
                    final_msg = retry_msg
                    final_text = retry_text

    final_msg["content"] = final_text

    if isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
        if not final_text or _looks_like_tool_dump(final_text):
            task_id_scored = last_score_result.get("task_id") if isinstance(last_score_result, dict) else None
            task_obj_local = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

            if task_obj_local and task_obj_local.get("type") == "theory" and bool(last_score_result.get("is_final")):
                final_text = _strip_trailing_tool_dump(final_text or "")
            else:
                final_text = _score_feedback(last_score_result)

    final_text = (final_text or "").strip()
    if isinstance(last_score_result, dict) and last_score_result.get("ok") is True:
        task_id_scored = last_score_result.get("task_id")
        task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None
        if task_obj and task_obj.get("type") == "theory" and bool(last_score_result.get("is_final")):
            theory_contract = build_theory_final_message_contract(task_obj, last_score_result)
            if theory_contract:
                final_text = sanitize_theory_final_message(final_text, theory_contract)
    if final_text and not has_model_messages:
        final_text = _ensure_first_model_greeting(final_text, session)
    final_text = _strip_intro(final_text, has_model_messages).strip()

    final_msg["content"] = final_text or ""

    db.add(
        models.Message(
            session_id=session_id,
            sender="model",
            text=final_msg.get("content") or "",
        )
    )
    db.commit()

    return {"message": final_msg}
