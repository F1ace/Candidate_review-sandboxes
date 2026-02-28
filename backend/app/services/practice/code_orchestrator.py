from __future__ import annotations

import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from ... import models
from .workflow import CODE_PIPELINE, CodeWorkflowState


def _extract_candidate_code(instruction: str) -> str:
    markers = ("КОД КАНДИДАТА:", "CODE:")
    for marker in markers:
        if marker in instruction:
            return instruction.split(marker, 1)[1].strip()
    return ""


def _parse_tool_call_args(tc: dict[str, Any]) -> dict[str, Any]:
    try:
        args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    return args


def _tool_name(tc: dict[str, Any]) -> str:
    return (tc.get("function") or {}).get("name") or ""


def _tools_subset(tools: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    allowed = set(names)
    return [t for t in tools if ((t.get("function") or {}).get("name") in allowed)]


def _pipeline_text() -> str:
    return " -> ".join(CODE_PIPELINE)


def _next_step_hint(next_tool: str) -> str:
    idx = CODE_PIPELINE.index(next_tool) + 1
    if next_tool == "run_code":
        return f"{idx}) run_code(language, code=<harness_code>)"
    if next_tool == "score_task":
        return f"{idx}) score_task(task_id, points, comment)"
    return f"{idx}) {next_tool}"


def _autofinal_summary(state: CodeWorkflowState) -> str:
    report = state.artifacts.get("run_report") or {}
    passrate = float(report.get("passrate") or 0.0)
    score = state.artifacts.get("score_result") or {}
    points = score.get("points")

    summary = "Проверка завершена. "
    if report:
        summary += f"Passrate: {passrate:.2%}. "
    if points is not None:
        summary += f"Оценка: {points}."
    return summary.strip()

def run_practice_code_review(
    *,
    session: models.Session,
    db: Session,
    instruction: str,
    task_id: str,
    tools: list[dict[str, Any]],
    chat: Callable[..., dict[str, Any]],
    build_system_prompt: Callable[[models.Session, bool], str],
    conversation_snapshot: Callable[[models.Session, list[models.Message]], str],
    convert_history: Callable[[list[models.Message]], list[dict[str, Any]]],
    extract_inline_tool_call: Callable[[str], tuple[str, dict[str, Any]] | None],
    dispatch_tool_call: Callable[[models.Session, dict[str, Any], Session], dict[str, Any]],
    get_task_by_id: Callable[[models.Scenario, str], dict[str, Any] | None],
    logger: logging.Logger,
    max_iters: int = 10,
) -> dict[str, Any]:
    history_db = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at)
        .all()
    )

    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).count() > 0

    system_prompt = build_system_prompt(session, rag_available)
    snapshot = conversation_snapshot(session, history_db)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    messages.extend(convert_history(history_db))
    messages.append(
        {
            "role": "system",
            "content": (
                "PRACTICE_MODE. Candidate already submitted code. "
                "You must finish the full tool pipeline and only then provide final feedback."
            ),
        }
    )
    messages.append({"role": "user", "content": instruction})

    candidate_code = _extract_candidate_code(instruction)
    if not candidate_code.strip():
        return {
            "reply": "Внутренняя ошибка: candidate_code не извлечен из instruction.",
            "tool_results": [],
        }

    task = get_task_by_id(session.scenario, task_id) or {}
    state = CodeWorkflowState(max_points=float(task.get("max_points") or 0.0))
    tool_results_for_ui: list[dict[str, Any]] = []

    final_msg: dict[str, Any] | None = None

    for _ in range(max_iters):
        allowed_tools = state.allowed_tools()
        toolset = _tools_subset(tools, allowed_tools)
        resp = chat(messages, tools=toolset)

        assistant_msg = resp["choices"][0]["message"]
        messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            content = assistant_msg.get("content") or ""
            inline = extract_inline_tool_call(content)
            if inline:
                tool_name, args = inline
                tool_calls = [
                    {
                        "id": "inline_toolcall",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                ]
                assistant_msg["content"] = None

        if not tool_calls:
            content = (assistant_msg.get("content") or "").strip()
            if state.is_complete():
                if content:
                    final_msg = assistant_msg
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": "Сформируй финальный ответ по результатам проверки и выставленной оценке.",
                    }
                )
                continue

            next_tool = state.next_required_tool()
            assert next_tool is not None
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "СТОП. Пайплайн не завершен. "
                        f"Обязательный порядок: {_pipeline_text()}. "
                        f"Следующий шаг: {_next_step_hint(next_tool)}."
                    ),
                }
            )
            continue

        tool_messages: list[dict[str, Any]] = []
        retry_tools = False

        for tc in tool_calls:
            name = _tool_name(tc)
            tc_id = tc.get("id") or f"{name}_call"
            next_tool = state.next_required_tool()

            if not next_tool:
                retry_tools = True
                messages.append(
                    {
                        "role": "user",
                        "content": "Пайплайн уже завершен. Сформируй итоговый ответ без новых tool-вызовов.",
                    }
                )
                continue

            if name != next_tool:
                retry_tools = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"СТОП. Сейчас разрешен только следующий шаг: {_next_step_hint(next_tool)}. "
                            f"Ты попытался вызвать '{name}'."
                        ),
                    }
                )
                continue

            args = _parse_tool_call_args(tc)
            prepared_args, arg_error = state.prepare_args(name, args, task_id=task_id, candidate_code=candidate_code)
            if arg_error:
                retry_tools = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"СТОП. Невозможно выполнить шаг {name}: {arg_error}. "
                            f"Проверь предыдущие шаги и повтори {_next_step_hint(next_tool)}."
                        ),
                    }
                )
                continue

            tc["function"]["arguments"] = json.dumps(prepared_args, ensure_ascii=False)

            try:
                result = dispatch_tool_call(session, tc, db)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Tool failed: %s", name)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            tool_results_for_ui.append({"name": name, "result": result})
            db.add(
                models.Message(
                    session_id=session.id,
                    sender="tool",
                    text=f"{name} -> {result}",
                    task_id=task_id,
                )
            )

            ok, reason = state.mark_result(name, result if isinstance(result, dict) else {"error": "non-dict tool result"})
            if not ok:
                retry_tools = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Инструмент {name} вернул некорректный результат: {reason}. "
                            f"Повтори шаг {_next_step_hint(next_tool)}."
                        ),
                    }
                )

            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        if retry_tools:
            continue

        messages.extend(tool_messages)

    if final_msg is None and not state.is_complete():
        auto_error: str | None = None
        for _ in range(len(CODE_PIPELINE)):
            next_tool = state.next_required_tool()
            if not next_tool:
                break
            prepared_args, arg_error = state.prepare_args(next_tool, {}, task_id=task_id, candidate_code=candidate_code)
            if arg_error:
                auto_error = arg_error
                break
            tc = {
                "id": f"auto_{next_tool}",
                "type": "function",
                "function": {
                    "name": next_tool,
                    "arguments": json.dumps(prepared_args, ensure_ascii=False),
                },
            }
            try:
                result = dispatch_tool_call(session, tc, db)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Auto tool failed: %s", next_tool)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            tool_results_for_ui.append({"name": next_tool, "result": result})
            db.add(
                models.Message(
                    session_id=session.id,
                    sender="tool",
                    text=f"{next_tool} -> {result}",
                    task_id=task_id,
                )
            )
            ok, reason = state.mark_result(next_tool, result if isinstance(result, dict) else {"error": "non-dict tool result"})
            if not ok:
                auto_error = reason or "unknown auto-step error"
                break

        if not state.is_complete():
            final_msg = {
                "role": "assistant",
                "content": (
                    "Проверка не завершена автоматически. "
                    f"Статус: {state.short_status()}. "
                    f"Причина: {auto_error or 'unknown error'}"
                ),
            }

    if final_msg is None:
        final_msg = {
            "role": "assistant",
            "content": _autofinal_summary(state),
        }

    content = final_msg.get("content") or ""
    db.add(models.Message(session_id=session.id, sender="system", text=content, task_id=task_id))
    db.commit()

    return {"reply": content, "tool_results": tool_results_for_ui}
