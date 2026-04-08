from __future__ import annotations

import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from ... import models
from .workflow import SQL_PIPELINE, SqlWorkflowState


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
    return " -> ".join(SQL_PIPELINE)


def _next_step_hint(next_tool: str) -> str:
    idx = SQL_PIPELINE.index(next_tool) + 1
    if next_tool == "run_sql":
        return f"{idx}) run_sql(task_id, query)"
    if next_tool == "score_task":
        return f"{idx}) score_task(task_id, points, comment)"
    return f"{idx}) {next_tool}"


def _sql_score_task_first_call_prompt(state: SqlWorkflowState) -> str:
    report = state.artifacts.get("run_report") or {}
    success = bool(report.get("success"))
    row_count = int(report.get("row_count") or 0)
    error = report.get("error")
    columns = report.get("columns") or []

    columns_preview = ", ".join(map(str, columns[:10])) if columns else "нет"

    status_line = "SQL выполнился успешно." if success else "SQL выполнился с ошибкой."
    error_line = f"Ошибка: {error}" if error else "Ошибка: нет"

    return (
        "Сейчас нужно СРАЗУ корректно вызвать score_task.\n"
        f"points должен быть числом от 0 до {int(round(state.max_points or 0))}.\n"
        "comment должен содержать РОВНО 4 заполненные секции:\n\n"
        "Корректность: [что верно или неверно в результате выполнения SQL]\n"
        "Качество решения: [структура запроса, читаемость, понятность, аккуратность]\n"
        "Работа с SQL: [фильтрация, join, group by, агрегации, оконные функции, обработка кейсов]\n"
        "Что можно улучшить: [1-3 конкретных улучшения]\n\n"
        f"{status_line}\n"
        f"Возвращено строк: {row_count}\n"
        f"Колонки: {columns_preview}\n"
        f"{error_line}\n\n"
        "Правила:\n"
        "- все 4 секции обязательны;\n"
        "- ни одна секция не должна быть пустой;\n"
        "- не используй квадратные скобки в финальном тексте;\n"
        "- points передай отдельно, не в comment;\n"
        "- после этого вызови score_task."
    )


def _sql_score_task_retry_template(state: SqlWorkflowState) -> str:
    report = state.artifacts.get("run_report") or {}
    success = bool(report.get("success"))
    row_count = int(report.get("row_count") or 0)
    error = report.get("error")
    columns = report.get("columns") or []

    columns_preview = ", ".join(map(str, columns[:10])) if columns else "нет"
    status_line = "SQL выполнился успешно." if success else "SQL выполнился с ошибкой."
    error_line = f"Ошибка: {error}" if error else "Ошибка: нет"

    return (
        "Предыдущий score_task не прошёл валидацию.\n"
        "Нужно НЕМЕДЛЕННО повторить только вызов score_task.\n"
        "Не пиши финальный ответ кандидату, пока score_task не будет принят.\n\n"
        "Исправь только comment.\n"
        "points передай отдельно.\n"
        "comment должен содержать СТРОГО 4 непустые секции:\n\n"
        "Корректность: [объясни, что верно или неверно по результату выполнения SQL]\n"
        "Качество решения: [оцени структуру запроса, читаемость и понятность]\n"
        "Работа с SQL: [оцени использование SQL-конструкций и логику выборки]\n"
        "Что можно улучшить: [1-3 конкретных улучшения]\n\n"
        f"{status_line}\n"
        f"Возвращено строк: {row_count}\n"
        f"Колонки: {columns_preview}\n"
        f"{error_line}\n\n"
        "Правила:\n"
        "- все 4 секции обязательны;\n"
        "- ни одна секция не должна быть пустой;\n"
        "- нельзя использовать квадратные скобки в финальном тексте;\n"
        "- нельзя писать шаблонные фразы и инструкции;\n"
        "- после исправления верни только tool call score_task."
    )


def run_practice_sql_review(
    *,
    session: models.Session,
    db: Session,
    instruction: str,
    task_id: str,
    candidate_query: str,
    tools: list[dict[str, Any]],
    chat: Callable[..., dict[str, Any]],
    build_system_prompt: Callable[[models.Session, bool], str],
    conversation_snapshot: Callable[[models.Session, list[models.Message]], str],
    extract_inline_tool_call: Callable[[str], tuple[str, dict[str, Any]] | None],
    dispatch_tool_call: Callable[[models.Session, dict[str, Any], Session], dict[str, Any]],
    get_task_by_id: Callable[[models.Scenario, str], dict[str, Any] | None],
    logger: logging.Logger,
    max_iters: int = 10,
) -> dict[str, Any]:
    history_db = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
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

    system_prompt = build_system_prompt(session, rag_available)
    snapshot = conversation_snapshot(session, history_db)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
        {
            "role": "system",
            "content": (
                "PRACTICE_MODE_SQL.\n"
                "Сейчас идет ТОЛЬКО проверка SQL-задания.\n"
                "Не анализируй Python-код и не ищи candidate_code.\n"
                "Нельзя писать JSON, tool-dump, schema, служебные поля, raw tool result или текст вида score_task -> {...}.\n"
                "Обязательный порядок действий:\n"
                "1) вызвать run_sql\n"
                "2) получить результат выполнения SQL\n"
                "3) вызвать score_task\n"
                "4) только после этого дать финальный комментарий по SQL-решению.\n"
                "При выставлении оценки через score_task:\n"
                "- опирайся на результат выполнения SQL и сам текст запроса кандидата;\n"
                "- оцени не только факт выполнения, но и корректность логики решения;\n"
                "- учитывай читаемость, структуру и уместность SQL-конструкций;\n"
                "- не вставляй шаблонные фразы, квадратные скобки и текст-заглушки;\n"
                "- каждый раздел комментария должен быть заполнен осмысленным текстом.\n"
                "Финальный ответ должен содержать:\n"
                "- итоговый балл,\n"
                "- вывод по корректности,\n"
                "- комментарий по качеству решения,\n"
                "- замечание по работе с SQL,\n"
                "- 1-3 конкретных улучшения.\n"
                "Не выводи служебные размышления."
            ),
        },
        {"role": "user", "content": instruction},
    ]

    if not (candidate_query or "").strip():
        return {
            "reply": "Внутренняя ошибка: candidate_query не передан в SQL orchestrator.",
            "tool_results": [],
        }

    task = get_task_by_id(session.scenario, task_id) or {}
    state = SqlWorkflowState(max_points=float(task.get("max_points") or 0.0))
    tool_results_for_ui: list[dict[str, Any]] = []

    final_msg: dict[str, Any] | None = None
    last_score_result: dict[str, Any] | None = None
    max_points = int(task.get("max_points", 0) or 0)

    for _ in range(max_iters):
        allowed_tools = state.allowed_tools()
        toolset = _tools_subset(tools, allowed_tools)
        resp = chat(messages, tools=toolset)

        assistant_msg = resp["choices"][0]["message"]

        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            content = assistant_msg.get("content") or ""
            inline = extract_inline_tool_call(content)
            if inline:
                tool_name, args = inline
                tool_calls = [
                    {
                        "id": "inline_sql_toolcall",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                ]
                assistant_msg["content"] = None
                assistant_msg["tool_calls"] = tool_calls

        messages.append(assistant_msg)

        if not tool_calls:
            content = (assistant_msg.get("content") or "").strip()
            if state.is_complete():
                if content:
                    final_msg = assistant_msg
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": "Сформируй финальный ответ по результатам SQL-проверки и выставленной оценке.",
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
            prepared_args, arg_error = state.prepare_args(
                name,
                args,
                task_id=task_id,
                candidate_query=candidate_query,
            )
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
                logger.exception("SQL tool failed: %s", name)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            tool_results_for_ui.append({"tool": name, "result": result})
            db.add(
                models.Message(
                    session_id=session.id,
                    sender="tool",
                    text=f"{name} -> {result}",
                    task_id=task_id,
                )
            )
            db.commit()

            ok, reason = state.mark_result(
                name,
                result if isinstance(result, dict) else {"error": "non-dict tool result"},
            )
            if not ok:
                retry_tools = True

                retry_message = (
                    f"Инструмент {name} вернул некорректный результат: {reason}. "
                    f"Повтори шаг {_next_step_hint(next_tool)}."
                )

                if name == "score_task" and reason:
                    if (
                        "Practice comment does not match required template" in reason
                        or "contains placeholders or template instructions" in reason
                        or "Practice comment has empty sections" in reason
                    ):
                        retry_message = (
                            f"Инструмент {name} вернул некорректный результат: {reason}.\n\n"
                            f"{_sql_score_task_retry_template(state)}"
                        )

                messages.append(
                    {
                        "role": "user",
                        "content": retry_message,
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

        if (
            state.next_required_tool() == "score_task"
            and state.artifacts.get("run_report")
            and not state.artifacts.get("score_result")
        ):
            messages.append(
                {
                    "role": "user",
                    "content": _sql_score_task_first_call_prompt(state),
                }
            )
            continue

        if state.is_complete():
            last_score_result = state.artifacts.get("score_result")
            messages.append(
                {
                    "role": "user",
                    "content": "Пайплайн завершен. Теперь сформируй финальный ответ кандидату без новых tool-вызовов.",
                }
            )
            continue

    reply = ""
    if final_msg:
        reply = (final_msg.get("content") or "").strip()

    fallback_reply = (
        "Корректность: Не удалось получить финальный ответ модели.\n"
        "Качество решения: Автоматическая часть проверки выполнена, но текстовый ответ не завершён.\n"
        "Работа с SQL: SQL review loop не дошёл до корректного финального сообщения.\n"
        "Что можно улучшить: Повторно запустить проверку и посмотреть tool trace."
    )

    if last_score_result and last_score_result.get("ok"):
        points = last_score_result.get("points")
        comment = (last_score_result.get("comment") or "").strip()

        if points is None and (reply or fallback_reply).strip():
            reply = (reply or fallback_reply).strip()
        else:
            score_line = (
                f"Оценка: {int(round(float(points)))}/{max_points}"
                if points is not None
                else f"Оценка: не выставлена из {max_points}"
            )

            parts = [score_line]
            if comment:
                parts.extend(["", comment])
            elif (reply or fallback_reply).strip():
                parts.extend(["", (reply or fallback_reply).strip()])
            reply = "\n".join(parts).strip()
    elif not reply:
        reply = fallback_reply

    return {
        "reply": reply,
        "tool_results": tool_results_for_ui,
    }