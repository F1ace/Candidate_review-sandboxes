from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from sqlalchemy.orm import Session

from ... import models
from .workflow import SQL_PIPELINE, SqlWorkflowState, normalize_sql_practice_comment

_SQL_REVIEW_HEADERS: tuple[str, ...] = (
    "Корректность:",
    "Качество решения:",
    "Работа с SQL:",
    "Что можно улучшить:",
)

_SQL_LEADING_SCORE_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?(?:оценка|балл)(?:\*\*)?\s*:\s*(?:не выставлена.*|\d+(?:[.,]\d+)?\s*/\s*\d+)\s*$",
    re.IGNORECASE,
)
_SQL_LEADING_POINTS_LINE_RE = re.compile(
    r"^\s*points?\s*:\s*\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?\s*$",
    re.IGNORECASE,
)
_SQL_LEADING_COMMENT_LABEL_RE = re.compile(
    r"^\s*(?:comment|комментарий)\s*:\s*(.*)\s*$",
    re.IGNORECASE,
)


def _extract_points_from_plain_feedback(content: str) -> float | None:
    text = str(content or "").strip()
    if not text:
        return None

    match = re.search(r"(?i)(?:оценка|балл)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*/\s*\d+", text)
    if not match:
        return None

    raw_points = match.group(1).replace(",", ".")
    try:
        return float(raw_points)
    except Exception:
        return None


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


def _count_sql_headers(content: str) -> int:
    lowered = str(content or "").lower()
    if not lowered:
        return 0
    return sum(1 for header in _SQL_REVIEW_HEADERS if header.lower() in lowered)


def _has_structured_sql_comment(content: str) -> bool:
    normalized = normalize_sql_practice_comment(
        str(content or ""),
        points=0,
        max_points=0,
    )
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    section_values: dict[str, str] = {}

    for line in lines:
        for header in _SQL_REVIEW_HEADERS:
            if line.startswith(header):
                section_values[header] = line[len(header):].strip()
                break

    return all(section_values.get(header) for header in _SQL_REVIEW_HEADERS)


def _extract_structured_sql_comment(content: str) -> str | None:
    raw = str(content or "").strip()
    if not raw:
        return None

    normalized = normalize_sql_practice_comment(
        raw,
        points=0,
        max_points=0,
    )
    if not _has_structured_sql_comment(normalized):
        return None

    return normalized


def _looks_like_plain_sql_feedback(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False

    present_headers = _count_sql_headers(text)
    has_score = _extract_points_from_plain_feedback(text) is not None

    if present_headers >= 2:
        return True
    if present_headers >= 1 and has_score:
        return True
    if has_score and len(text) >= 120:
        return True
    if len(text) >= 200 and any(marker in text.lower() for marker in ("sql", "запрос", "результат", "ошибка", "выборк")):
        return True

    return False


def _coerce_plain_sql_feedback_to_score_task(
    assistant_msg: dict[str, Any],
    *,
    tool_call_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tool_calls = assistant_msg.get("tool_calls") or []
    if tool_calls:
        return assistant_msg, list(tool_calls)

    content = (assistant_msg.get("content") or "").strip()
    if not content or not _looks_like_plain_sql_feedback(content):
        return assistant_msg, []

    structured_comment = _extract_structured_sql_comment(content)
    if not structured_comment:
        return assistant_msg, []

    args: dict[str, Any] = {"comment": structured_comment}
    inferred_points = _extract_points_from_plain_feedback(content)
    if inferred_points is not None:
        args["points"] = inferred_points

    tool_calls = [
        {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": "score_task",
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }
    ]
    assistant_msg["content"] = None
    assistant_msg["tool_calls"] = tool_calls
    return assistant_msg, tool_calls


def _normalize_model_sql_reply(content: str) -> str:
    lines = str(content or "").splitlines()
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue

        if _SQL_LEADING_SCORE_LINE_RE.match(first) or _SQL_LEADING_POINTS_LINE_RE.match(first):
            lines.pop(0)
            continue

        comment_match = _SQL_LEADING_COMMENT_LABEL_RE.match(first)
        if comment_match:
            tail = comment_match.group(1).strip()
            if tail:
                lines[0] = tail
            else:
                lines.pop(0)
            continue

        break

    return "\n".join(lines).strip()


def _sql_reply_needs_fallback(content: str) -> bool:
    stripped = str(content or "").strip()
    if not stripped:
        return True

    return (
        stripped.lstrip().startswith("{")
        or "score_task ->" in stripped
        or "run_sql ->" in stripped
        or "tool call" in stripped.lower()
        or (
            _count_sql_headers(stripped) == 0
            and len(stripped) < 90
            and stripped.count(".") < 2
            and stripped.count("\n") < 2
        )
    )


def _sql_final_reply_prompt(
    state: SqlWorkflowState,
    *,
    strict_retry: bool = False,
) -> str:
    score = state.artifacts.get("score_result") or {}
    points = score.get("points")
    comment = (score.get("comment") or "").strip()
    max_points = int(round(state.max_points or 0)) or 10
    score_line = f"Принятая оценка: {points}/{max_points}.\n" if points is not None else ""
    comment_line = f"Принятый комментарий score_task:\n{comment}\n\n" if comment else ""

    base_prompt = (
        "SQL-проверка уже завершена. "
        "Теперь напиши финальный отзыв кандидату обычным текстом. "
        "Не вызывай инструменты. "
        "Не пиши JSON, tool-dump, schema или служебные поля. "
        "Используй принятый score_task.comment как источник фактов, но не копируй его дословно: "
        "перепиши своими словами как живой финальный отзыв кандидату. "
        "Сохрани те же 4 смысловых блока:\n"
        "Корректность:\n"
        "Качество решения:\n"
        "Работа с SQL:\n"
        "Что можно улучшить:\n"
        "Балл отдельной строкой не дублируй: он уже показывается в UI.\n\n"
        f"{score_line}"
        f"{comment_line}"
    )
    if not strict_retry:
        return base_prompt

    return (
        base_prompt
        + "СТОП. В прошлый раз ответ не подошёл. "
        "Сейчас нужен только финальный комментарий кандидату по уже принятому score_task. "
        "Не повторяй служебный статус, не копируй comment дословно и не печатай технические причины ошибок. "
        "Сохрани 4 содержательных блока: Корректность, Качество решения, Работа с SQL, Что можно улучшить."
    )


def _sql_score_comment_prompt(
    state: SqlWorkflowState,
    *,
    draft_feedback: str | None = None,
    strict_retry: bool = False,
) -> str:
    report = state.artifacts.get("run_report") or {}
    success = bool(report.get("success"))
    row_count = int(report.get("row_count") or 0)
    error = report.get("error")
    columns = report.get("columns") or []

    columns_preview = ", ".join(map(str, columns[:10])) if columns else "нет"
    status_line = "SQL выполнился успешно." if success else "SQL выполнился с ошибкой."
    error_line = f"Ошибка: {error}" if error else "Ошибка: нет"

    draft_block = ""
    if draft_feedback:
        draft_block = (
            "Ниже черновик предыдущего ответа модели. "
            "Используй его только как материал и перепиши в валидный финальный comment:\n"
            f"{draft_feedback.strip()}\n\n"
        )

    prompt = (
        "Нужен только готовый текст поля comment для score_task по SQL-задаче. "
        "Не вызывай инструменты. Не пиши points, JSON, schema, markdown fences или служебные поля.\n"
        "Верни ровно 4 непустые секции с такими заголовками:\n"
        "Корректность:\n"
        "Качество решения:\n"
        "Работа с SQL:\n"
        "Что можно улучшить:\n\n"
        f"{status_line}\n"
        f"Возвращено строк: {row_count}\n"
        f"Колонки: {columns_preview}\n"
        f"{error_line}\n\n"
        f"{draft_block}"
        "Комментарий должен быть осмысленным, финальным и опираться на результат выполнения SQL. "
        "Все 4 секции обязательны и не должны быть пустыми. "
        "Нельзя использовать квадратные скобки, слова 'заполни', 'шаблон' или 'если применимо'."
    )
    if not strict_retry:
        return prompt

    return (
        prompt
        + " В прошлый раз ответ не подошёл. "
        "Сейчас верни только четыре заполненные секции comment без пояснений сверху и снизу."
    )


def _request_model_sql_score_comment(
    state: SqlWorkflowState,
    *,
    messages: list[dict[str, Any]],
    chat: Callable[..., dict[str, Any]],
    draft_feedback: str | None = None,
) -> str | None:
    for attempt in range(3):
        messages.append(
            {
                "role": "user",
                "content": _sql_score_comment_prompt(
                    state,
                    draft_feedback=draft_feedback,
                    strict_retry=attempt > 0,
                ),
            }
        )
        resp = chat(messages, tools=[])
        assistant_msg = resp["choices"][0]["message"]
        messages.append(assistant_msg)

        comment = _extract_structured_sql_comment(assistant_msg.get("content") or "")
        if comment:
            return comment

    return None


def _sql_reply_from_score(score_result: dict[str, Any] | None, *, max_points: int) -> str:
    score = score_result or {}
    points = score.get("points")
    comment = (score.get("comment") or "").strip()

    score_line = (
        f"Оценка: {int(round(float(points)))}/{max_points}"
        if points is not None
        else f"Оценка: не выставлена из {max_points}"
    )

    parts = [score_line]
    if comment:
        parts.extend(["", comment])
    return "\n".join(parts).strip()


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
                "- отдельной строкой не дублируй балл: он уже показывается в UI,\n"
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
    backend_generated_reply = False
    pending_score_task_recovery_error: str | None = None
    last_score_feedback_draft: str | None = None

    try:
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

            if (
                not tool_calls
                and state.next_required_tool() == "score_task"
                and state.artifacts.get("run_report")
            ):
                plain_feedback = (assistant_msg.get("content") or "").strip()
                if plain_feedback and _looks_like_plain_sql_feedback(plain_feedback):
                    last_score_feedback_draft = plain_feedback
                assistant_msg, tool_calls = _coerce_plain_sql_feedback_to_score_task(
                    assistant_msg,
                    tool_call_id="plain_feedback_sql_score_task",
                )

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
                if name == "score_task":
                    raw_comment = str(args.get("comment") or "").strip()
                    if raw_comment:
                        last_score_feedback_draft = raw_comment
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
                continue

        if final_msg is None and state.next_required_tool() == "score_task" and state.artifacts.get("run_report"):
            pending_score_task_recovery_error = (
                "Проверка SQL не завершена автоматически. "
                f"Статус: {state.short_status()}. "
                "Причина: model did not complete required score_task step"
            )
            recovery_prompts = [
                (
                    "SQL-проверка уже выполнена, результат run_sql получен. "
                    "Теперь нужно НЕМЕДЛЕННО вызвать только score_task. "
                    "Не пиши финальный ответ кандидату. "
                    "Верни только tool call score_task с валидным comment.\n\n"
                    f"{_sql_score_task_first_call_prompt(state)}"
                ),
                (
                    "СТОП. Сейчас нужен только вызов score_task. "
                    "Нельзя писать обычный текст вместо tool call. "
                    "Повтори и верни только score_task."
                ),
                (
                    "СТОП. До завершения SQL-пайплайна остался только score_task. "
                    "Нужен один корректный вызов score_task и больше ничего."
                ),
            ]
            assistant_msg = {"role": "assistant", "content": ""}
            tool_calls = []

            for attempt, prompt in enumerate(recovery_prompts):
                messages.append({"role": "user", "content": prompt})
                resp = chat(messages, tools=_tools_subset(tools, ["score_task"]))
                assistant_msg = resp["choices"][0]["message"]

                tool_calls = assistant_msg.get("tool_calls") or []
                if not tool_calls:
                    content = assistant_msg.get("content") or ""
                    inline = extract_inline_tool_call(content)
                    if inline:
                        tool_name, args = inline
                        tool_calls = [
                            {
                                "id": f"inline_sql_score_task_recovery_{attempt}",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(args, ensure_ascii=False),
                                },
                            }
                        ]
                        assistant_msg["content"] = None
                        assistant_msg["tool_calls"] = tool_calls

                if not tool_calls:
                    plain_feedback = (assistant_msg.get("content") or "").strip()
                    if plain_feedback and _looks_like_plain_sql_feedback(plain_feedback):
                        last_score_feedback_draft = plain_feedback
                    assistant_msg, tool_calls = _coerce_plain_sql_feedback_to_score_task(
                        assistant_msg,
                        tool_call_id=f"plain_feedback_sql_score_task_recovery_{attempt}",
                    )

                messages.append(assistant_msg)
                if tool_calls:
                    break

            if tool_calls:
                retry_tools = False
                tool_messages = []

                for tc in tool_calls:
                    name = _tool_name(tc)
                    tc_id = tc.get("id") or f"{name}_call"

                    if name != "score_task":
                        retry_tools = True
                        messages.append(
                            {
                                "role": "user",
                                "content": "Сейчас разрешён только вызов score_task.",
                            }
                        )
                        continue

                    args = _parse_tool_call_args(tc)
                    raw_comment = str(args.get("comment") or "").strip()
                    if raw_comment:
                        last_score_feedback_draft = raw_comment
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
                                "content": f"Невозможно выполнить score_task: {arg_error}",
                            }
                        )
                        continue

                    tc["function"]["arguments"] = json.dumps(prepared_args, ensure_ascii=False)

                    try:
                        result = dispatch_tool_call(session, tc, db)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("SQL tool failed during recovery: %s", name)
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
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"Инструмент score_task вернул некорректный результат: {reason}.\n\n"
                                    f"{_sql_score_task_retry_template(state)}"
                                ),
                            }
                        )
                    else:
                        pending_score_task_recovery_error = None
                        tool_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )

                if not retry_tools:
                    messages.extend(tool_messages)

        if (
            final_msg is None
            and not state.is_complete()
            and state.next_required_tool() == "score_task"
            and state.artifacts.get("run_report")
            and not state.artifacts.get("score_result")
        ):
            generated_comment = _request_model_sql_score_comment(
                state,
                messages=messages,
                chat=chat,
                draft_feedback=last_score_feedback_draft,
            )
            if generated_comment:
                score_args: dict[str, Any] = {"comment": generated_comment}
                inferred_points = _extract_points_from_plain_feedback(last_score_feedback_draft or "")
                if inferred_points is not None:
                    score_args["points"] = inferred_points

                prepared_args, arg_error = state.prepare_args(
                    "score_task",
                    score_args,
                    task_id=task_id,
                    candidate_query=candidate_query,
                )
            else:
                prepared_args, arg_error = {}, "model did not produce valid score_task comment"

            if not arg_error:
                tc = {
                    "id": "model_generated_sql_score_task_after_recovery",
                    "type": "function",
                    "function": {
                        "name": "score_task",
                        "arguments": json.dumps(prepared_args, ensure_ascii=False),
                    },
                }
                try:
                    result = dispatch_tool_call(session, tc, db)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Model-generated SQL score_task failed after recovery: %s", exc)
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

                tool_results_for_ui.append({"tool": "score_task", "result": result})
                db.add(
                    models.Message(
                        session_id=session.id,
                        sender="tool",
                        text=f"score_task -> {result}",
                        task_id=task_id,
                    )
                )
                db.commit()

                ok, _reason = state.mark_result(
                    "score_task",
                    result if isinstance(result, dict) else {"error": "non-dict tool result"},
                )
                if ok:
                    pending_score_task_recovery_error = None
                    last_score_feedback_draft = generated_comment
                    last_score_result = state.artifacts.get("score_result")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": "model_generated_sql_score_task_after_recovery",
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )

        if final_msg is None and state.is_complete():
            last_score_result = state.artifacts.get("score_result")

    except Exception as exc:  # noqa: BLE001
        logger.exception("Practice SQL review failed unexpectedly: %s", exc)
        reply = fallback_reply = (
            "Корректность: Не удалось получить финальный ответ модели.\n"
            "Качество решения: Автоматическая часть проверки выполнена, но текстовый ответ не завершён.\n"
            "Работа с SQL: SQL review loop не дошёл до корректного финального сообщения.\n"
            "Что можно улучшить: Повторно запустить проверку и посмотреть tool trace."
        )
        db.add(models.Message(session_id=session.id, sender="model", text=reply, task_id=task_id))
        db.commit()
        return {
            "reply": reply,
            "tool_results": tool_results_for_ui,
            "reply_source": "fallback",
        }

    reply = ""
    if final_msg:
        reply = _normalize_model_sql_reply((final_msg.get("content") or "").strip())
    elif state.is_complete():
        final_attempts = 0
        while final_attempts < 3:
            messages.append(
                {
                    "role": "user",
                    "content": _sql_final_reply_prompt(
                        state,
                        strict_retry=final_attempts > 0,
                    ),
                }
            )
            resp = chat(messages, tools=[])
            candidate_final_msg = resp["choices"][0]["message"]
            messages.append(candidate_final_msg)
            candidate_content = _normalize_model_sql_reply(candidate_final_msg.get("content") or "")
            final_msg = candidate_final_msg
            reply = candidate_content
            if not _sql_reply_needs_fallback(candidate_content):
                break
            final_attempts += 1

    fallback_reply = (
        "Корректность: Не удалось получить финальный ответ модели.\n"
        "Качество решения: Автоматическая часть проверки выполнена, но текстовый ответ не завершён.\n"
        "Работа с SQL: SQL review loop не дошёл до корректного финального сообщения.\n"
        "Что можно улучшить: Повторно запустить проверку и посмотреть tool trace."
    )

    last_score_result = last_score_result or (state.artifacts.get("score_result") or None)

    if last_score_result and last_score_result.get("ok"):
        if _sql_reply_needs_fallback(reply):
            fallback_from_score = _sql_reply_from_score(
                last_score_result,
                max_points=max_points,
            )
            reply = fallback_from_score or fallback_reply
            backend_generated_reply = True
    elif not reply:
        reply = fallback_reply
        backend_generated_reply = True

    if not reply and pending_score_task_recovery_error:
        reply = pending_score_task_recovery_error
        backend_generated_reply = True

    db.add(models.Message(session_id=session.id, sender="model", text=reply, task_id=task_id))
    db.commit()

    return {
        "reply": reply,
        "tool_results": tool_results_for_ui,
        "reply_source": "fallback" if backend_generated_reply else "model",
    }
