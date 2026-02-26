import json
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ... import models
from ...database import SessionLocal
from ...services.lm_client import lm_client
from .dispatch import _dispatch_tool_call
from .practice import _score_feedback
from .prompting import (
    _analyze_candidate_message,
    _build_system_prompt,
    _extract_inline_tool_call,
    _strip_intro,
    _strip_think,
)
from .router import logger
from .state import (
    _control_state,
    _conversation_snapshot,
    _convert_history,
    _first_practice_task,
    _get_task_by_id,
    _theory_is_complete,
    _theory_summary_text,
)
from .tools import TOOLS
def stream_model(session_id: str):
    """Stream tokens from LM Studio. Runs tool calls first, then streams/returns final answer."""
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
    # Pre-validate last candidate message for placeholders/offtopic
    last_msg = history_db[-1] if history_db else None
    if last_msg and last_msg.sender == "candidate":
        flags = _analyze_candidate_message(last_msg.text)
        if flags:
            warn = "Ответ не принят: дайте содержательный ответ по сути вопроса."
            if "code_in_chat" in flags or "sql_in_chat" in flags:
                warn = "Не вставляйте код/SQL в чат. Введите решение в редактор ниже и нажмите Submit."
            base_db.add(models.Message(session_id=session_id, sender="system", text=warn))
            base_db.commit()
            base_db.close()

            def reject_stream():
                yield "data: " + json.dumps({"type": "token", "content": warn}, ensure_ascii=False) + "\n\n"
                yield "data: " + json.dumps({"type": "done", "content": warn}, ensure_ascii=False) + "\n\n"

            return StreamingResponse(reject_stream(), media_type="text/event-stream")

    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = (
            base_db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).count() > 0
        )
    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    base_messages.extend(_convert_history(history_db))

    try:
        first_resp = lm_client.chat(base_messages, tools=TOOLS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("LM request failed before streaming")
        base_db.close()
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls")

    # Fallback: если tool_calls нет, но модель вывела tool-call текстом — распознаём и исполняем
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
            # чтобы этот мусор не улетел пользователю как "ответ модели"
            assistant_msg["content"] = None

    stream_messages = list(base_messages)
    tool_results_payload: list[dict[str, Any]] = []
    status_events: list[str] = []

    score_result_payload: dict[str, Any] | None = None
    transition_added = False
    if tool_calls:
        stream_messages.append(assistant_msg)

        for tc in tool_calls:
            fname = tc["function"]["name"]

            # распарсить args безопасно
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}

            task_id_for_db = args.get("task_id")

            if fname == "web_search":
                status_text = f"Ищем в интернете: {args.get('query', '')}"
                base_db.add(models.Message(session_id=session_id, sender="system", text=status_text))
                base_db.commit()
                status_events.append(status_text)

            # сначала вычисляем result (и не роняем стрим, если tool упал)
            try:
                result = _dispatch_tool_call(session, tc, base_db)
            except Exception as e:
                logger.exception("Tool failed: %s", fname)
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            if fname == "score_task":
                score_result_payload = result

                # Если это оценка теоретического задания и теория теперь завершена —
                # просим модель в финальном ответе подвести итог и переключить на практику.
                if not transition_added:
                    task_id_scored = args.get("task_id")
                    task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

                    if task_obj and task_obj.get("type") == "theory" and _theory_is_complete(session):
                        transition_added = True
                        summary = _theory_summary_text(session)
                        practice_task = _first_practice_task(session.scenario)

                        practice_title = practice_task.get("title") if practice_task else None
                        practice_id = practice_task.get("id") if practice_task else None
                        practice_type = practice_task.get("type") if practice_task else None
                        practice_desc = ""
                        if practice_task:
                            practice_desc = (
                                practice_task.get("description_for_candidate")
                                or practice_task.get("description")
                                or practice_task.get("prompt")
                                or ""
                            )

                        stream_messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "ТЕОРИЯ ЗАВЕРШЕНА.\n"
                                    "Запрещено: приветствия, повтор вашего плана, повтор формулировок теоретических вопросов.\n"
                                    "Нужно: 1) кратко сообщить итог теории, 2) объявить переход к практике, "
                                    "3) сказать, что пользователю нужно перейти на вкладку «Практика», вставить решение в редактор и нажать «Проверить моделью», "
                                    "4) назвать следующее практическое задание.\n\n"
                                    f"{summary}\n"
                                    f"Следующее практическое задание: {practice_id or ''} {practice_title or ''} (тип: {practice_type or ''}).\n"
                                    f"Описание: {practice_desc}\n"
                                ),
                            }
                        )

            # tool_call_id нужен модели для привязки результата к вызову
            tc_id = tc.get("id") or f"{fname}_call"

            # добавляем tool-result в историю для модели
            stream_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

            # сохраняем для записи в БД (sender=tool)
            tool_results_payload.append(
                {
                    "name": fname,
                    "result": result,
                    "sender": "tool",
                    "text": f"{fname} -> {result}",
                    "task_id": task_id_for_db,
                }
            )
    else:
        stream_messages = base_messages

    base_db.close()

    def event_stream():
        local_db = SessionLocal()
        local_session = local_db.get(models.Session, session_id)
        history_local = (
        local_db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at)
        .all()
    )
        control_state = _control_state(local_session, history_local)

        final_chunks: list[str] = []
        hidden_buffer = ""
        revealed = False
        saw_think = False
        fallback_text = _strip_think(assistant_msg.get("content"))
        # If the model only called score_task and stayed silent, prepare a minimal feedback
        if not fallback_text:
            score_calls = [t for t in tool_results_payload if t.get("name") == "score_task"]
            if score_calls:
                res = score_calls[-1].get("result") or {}
                fallback_text = _score_feedback(res)
        received_tokens = False
        final_text = ""
        try:
            for status_text in status_events:
                yield "data: " + json.dumps({"type": "token", "content": status_text}, ensure_ascii=False) + "\n\n"

            if tool_calls:
                try:
                    sync_resp = lm_client.chat(stream_messages, tools=[])
                    final_text = _strip_think(sync_resp["choices"][0]["message"].get("content"))
                except Exception:
                    final_text = fallback_text or ""
                if score_result_payload and (not final_text or final_text.strip() == fallback_text.strip()):
                    final_text = _score_feedback(score_result_payload)
                chunk_size = 120
                for i in range(0, len(final_text), chunk_size):
                    piece = final_text[i : i + chunk_size]
                    yield "data: " + json.dumps({"type": "token", "content": piece}, ensure_ascii=False) + "\n\n"
                    final_chunks.append(piece)
                final_text = "".join(final_chunks)
            else:
                for chunk in lm_client.stream_chat(stream_messages, tools=TOOLS):
                    if "<think>" in chunk:
                        saw_think = True
                    if not saw_think and not revealed:
                        revealed = True  # нет блока размышлений – стримим сразу
                    if saw_think and not revealed:
                        hidden_buffer += chunk
                        if "</think>" in hidden_buffer:
                            revealed = True
                            after = hidden_buffer.split("</think>", 1)[1]
                            hidden_buffer = ""
                            if after:
                                final_chunks.append(after)
                                yield "data: " + json.dumps({"type": "token", "content": after}, ensure_ascii=False) + "\n\n"
                                received_tokens = True
                        continue
                    final_chunks.append(chunk)
                    yield "data: " + json.dumps({"type": "token", "content": chunk}, ensure_ascii=False) + "\n\n"
                    received_tokens = True
                final_text = "".join(final_chunks)
                if not received_tokens and not final_text:
                    try:
                        sync_resp = lm_client.chat(stream_messages, tools=[])
                        final_text = _strip_think(sync_resp["choices"][0]["message"].get("content"))
                    except Exception:
                        final_text = fallback_text or ""

            for payload in tool_results_payload:
                msg = models.Message(
                    session_id=session_id,
                    sender=payload["sender"],
                    text=payload["text"],
                    task_id=payload.get("task_id"),
                )
                local_db.add(msg)

            if final_text:
                trimmed = _strip_intro(final_text, control_state.get("intro_done", False))
                local_db.add(models.Message(session_id=session_id, sender="model", text=trimmed))
            elif fallback_text:
                trimmed = _strip_intro(fallback_text, control_state.get("intro_done", False))
                local_db.add(models.Message(session_id=session_id, sender="model", text=trimmed))
                final_text = trimmed
            local_db.commit()
            yield "data: " + json.dumps({"type": "done", "content": final_text}, ensure_ascii=False) + "\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("LM streaming failed")
            local_db.add(
                models.Message(
                    session_id=session_id,
                    sender="system",
                    text=f"Ошибка сервиса LM Studio: {exc}",
                )
            )
            local_db.commit()
            yield "data: " + json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False) + "\n\n"
            if fallback_text:
                yield "data: " + json.dumps({"type": "done", "content": fallback_text}, ensure_ascii=False) + "\n\n"
        finally:
            local_db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")

