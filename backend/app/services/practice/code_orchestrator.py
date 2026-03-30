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

def _practice_fallback_feedback(state: CodeWorkflowState) -> str:
    report = state.artifacts.get("run_report") or {}
    score = state.artifacts.get("score_result") or {}

    tests_total = int(report.get("tests_total") or 0)
    tests_passed = int(report.get("tests_passed") or 0)
    points = score.get("points", 0)
    comment = (score.get("comment") or "").strip()

    parts = [
        f"Практическая проверка завершена.",
        f"",
        f"**Балл:** {points}",
        f"**Тесты:** пройдено {tests_passed} из {tests_total}.",
    ]

    if comment:
        parts.extend(["", comment])

    return "\n".join(parts)

def _practice_reply_from_score(state: CodeWorkflowState) -> str:
    score = state.artifacts.get("score_result") or {}
    points = score.get("points")
    comment = (score.get("comment") or "").strip()

    parts = ["Практическая проверка завершена."]

    if points is not None:
        parts.append(f"\nБалл: {points}/10")

    if comment:
        parts.append(f"\n{comment}")

    return "\n".join(parts)

def _score_task_first_call_prompt(state: CodeWorkflowState) -> str:
    report = state.artifacts.get("run_report") or {}
    tests_passed = int(report.get("tests_passed") or 0)
    tests_total = int(report.get("tests_total") or 0)

    failed_tests = []
    for test in report.get("test_results") or []:
        if not test.get("passed"):
            name = test.get("name") or test.get("code") or "unknown test"
            error = test.get("error") or "no details"
            failed_tests.append(f"- {name}: {error}")

    failed_tests_block = "\n".join(failed_tests) if failed_tests else "- нет"

    return (
        "Сейчас нужно СРАЗУ корректно вызвать score_task.\n"
        f"points должен быть числом от 0 до {int(round(state.max_points or 0))}.\n"
        "comment должен содержать РОВНО 4 заполненные секции:\n\n"
        "Корректность: [что работает, что не работает, опираясь на sandbox]\n"
        "Качество кода: [читаемость, структура, нейминг, обработка крайних случаев]\n"
        "Сложность и эффективность: [краткая оценка или фраза, что для этой задачи это несущественно]\n"
        "Что можно улучшить: [1-3 конкретных улучшения]\n\n"
        f"Тесты песочницы: пройдено {tests_passed} из {tests_total}.\n"
        "Упавшие тесты:\n"
        f"{failed_tests_block}\n\n"
        "Правила:\n"
        "- все 4 секции обязательны;\n"
        "- ни одна секция не должна быть пустой;\n"
        "- не используй квадратные скобки в финальном тексте;\n"
        "- points передай отдельно, не в comment;\n"
        "- после этого вызови score_task."
    )

def _score_task_retry_template(state: CodeWorkflowState) -> str:
    report = state.artifacts.get("run_report") or {}
    tests_passed = int(report.get("tests_passed") or 0)
    tests_total = int(report.get("tests_total") or 0)

    failed_tests = []
    for test in report.get("test_results") or []:
        if not test.get("passed"):
            name = test.get("name") or test.get("code") or "unknown test"
            error = test.get("error") or "no details"
            failed_tests.append(f"- {name}: {error}")

    failed_tests_block = "\n".join(failed_tests) if failed_tests else "- нет"

    return (
        "Предыдущий score_task не прошёл валидацию.\n"
        "Нужно НЕМЕДЛЕННО повторить только вызов score_task.\n"
        "Не пиши финальный ответ кандидату, пока score_task не будет принят.\n\n"
        "Исправь только comment.\n"
        "points передай отдельно.\n"
        "comment должен содержать СТРОГО 4 непустые секции:\n\n"
        "Корректность: [объясни, что работает и что не работает по результатам sandbox]\n"
        "Качество кода: [оцени читаемость, структуру, нейминг, обработку крайних случаев]\n"
        "Сложность и эффективность: [краткая оценка или явная фраза, что отдельные замечания несущественны]\n"
        "Что можно улучшить: [1-3 конкретных улучшения]\n\n"
        f"Тесты песочницы: пройдено {tests_passed} из {tests_total}.\n"
        "Упавшие тесты:\n"
        f"{failed_tests_block}\n\n"
        "Правила:\n"
        "- все 4 секции обязательны;\n"
        "- ни одна секция не должна быть пустой;\n"
        "- нельзя использовать квадратные скобки в финальном тексте;\n"
        "- нельзя писать шаблонные фразы и инструкции;\n"
        "- после исправления верни только tool call score_task."
    )

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
    ]
    messages.append(
        {
            "role": "system",
            "content": (
                "PRACTICE_MODE.\n"
                "Сейчас идет ТОЛЬКО проверка практического задания.\n"
                "Теоретический блок, приветствие, вопросы интервью и переходы по theory полностью запрещены.\n"
                "Если в истории есть старые сообщения theory, их нужно игнорировать.\n"
                "Нельзя писать JSON, tool-dump, schema, служебные поля, raw tool result или текст вида score_task -> {...}.\n"
                "Нельзя повторять вступление интервьюера.\n"
                "Нужно проверить уже присланный код кандидата по задаче coding.\n"
                "Обязательный порядок действий:\n"
                "1) вызвать run_code\n"
                "2) получить результат sandbox\n"
                "3) вызвать score_task\n"
                "4) только после этого дать финальный комментарий по практическому решению.\n"
                "При выставлении оценки через score_task:\n"
                "- опирайся на результаты sandbox, passrate и сам код кандидата;\n"
                "- оцени не только прохождение тестов, но и качество решения;\n"
                "- обязательно учитывай, какие именно тесты упали и какие ошибки вернул sandbox;\n"
                "- если несколько тестов падают по одной и той же причине, укажи предполагаемый дефект в логике кандидата;\n"
                "- учитывай читаемость, структуру, нейминг и обработку крайних случаев;\n"
                "- если это уместно, кратко оцени сложность и эффективность;\n"
                "- не вставляй шаблонные фразы, квадратные скобки и текст-заглушки;\n"
                "- каждый раздел комментария должен быть заполнен осмысленным текстом.\n"
                "Финальный ответ должен быть обычным текстом для кандидата.\n"
                "Финальный ответ обязан содержать:\n"
                "- итоговый балл,\n"
                "- краткий вывод по корректности решения,\n"
                "- комментарий по качеству кода,\n"
                "- при необходимости замечание по сложности/эффективности,\n"
                "- 1–3 конкретных улучшения.\n"
                "Не начинай теорию заново. Не выводи служебные размышления."
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
    backend_completed_pipeline = False
    backend_generated_reply = False

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
                        "id": "inline_toolcall",
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
                            f"{_score_task_retry_template(state)}"
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
                    "content": _score_task_first_call_prompt(state),
                }
            )
            continue

    if final_msg is None and not state.is_complete():
        auto_error: str | None = None
        for _ in range(len(CODE_PIPELINE)):
            next_tool = state.next_required_tool()
            if not next_tool:
                break

            if next_tool == "score_task":
                auto_error = "model did not produce score_task comment"
                break

            prepared_args, arg_error = state.prepare_args(
                next_tool,
                {},
                task_id=task_id,
                candidate_code=candidate_code,
            )
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

        if state.is_complete():
            backend_completed_pipeline = True

        if not state.is_complete():
            if not (
                state.next_required_tool() == "score_task"
                and state.artifacts.get("run_report")
            ):
                final_msg = {
                    "role": "assistant",
                    "content": (
                        "Проверка не завершена автоматически. "
                        f"Статус: {state.short_status()}. "
                        f"Причина: {auto_error or 'unknown error'}"
                    ),
                }

    if final_msg is None and state.next_required_tool() == "score_task" and state.artifacts.get("run_report"):
        if final_msg is None and not state.is_complete():
            final_msg = {
                "role": "assistant",
                "content": (
                    "Проверка не завершена автоматически. "
                    f"Статус: {state.short_status()}. "
                    "Причина: model did not complete required score_task step"
                ),
            }
        messages.append(
            {
                "role": "user",
                "content": (
                    "Проверка кода уже выполнена, результат sandbox получен. "
                    "Теперь нужно НЕМЕДЛЕННО вызвать только score_task. "
                    "Не пиши финальный ответ кандидату. "
                    "Верни только tool call score_task с валидным comment.\n\n"
                    f"{_score_task_first_call_prompt(state)}"
                ),
            }
        )

        resp = chat(messages, tools=_tools_subset(tools, ["score_task"]))
        assistant_msg = resp["choices"][0]["message"]
        tool_calls = assistant_msg.get("tool_calls") or []

        if not tool_calls:
            content = (assistant_msg.get("content") or "").strip()
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "СТОП. Сейчас нужен только вызов score_task. "
                        "Нельзя писать обычный текст вместо tool call. "
                        "Повтори и верни только score_task."
                    ),
                }
            )

            resp = chat(messages, tools=_tools_subset(tools, ["score_task"]))
            assistant_msg = resp["choices"][0]["message"]
            tool_calls = assistant_msg.get("tool_calls") or []
            messages.append(assistant_msg)

        messages.append(assistant_msg)

        if tool_calls:
            retry_tools = False
            tool_messages: list[dict[str, Any]] = []

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
                prepared_args, arg_error = state.prepare_args(
                    name,
                    args,
                    task_id=task_id,
                    candidate_code=candidate_code,
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
                                f"{_score_task_retry_template(state)}"
                            ),
                        }
                    )
                else:
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )

            if not retry_tools:
                messages.extend(tool_messages)

    if final_msg is None and state.is_complete():
        messages.append(
            {
                "role": "user",
                "content": (
                    "Проверка практического задания уже завершена. "
                    "Теперь напиши финальный отзыв кандидату обычным текстом. "
                    "Не вызывай инструменты. "
                    "Не пиши JSON, tool-dump, schema или служебные поля. "
                    "Ответ должен содержать итоговый балл, краткий вывод по корректности, "
                    "комментарий по качеству кода, замечание по сложности/эффективности при необходимости "
                    "и 1-3 конкретных улучшения."
                ),
            }
        )
        resp = chat(messages, tools=[])
        final_msg = resp["choices"][0]["message"]

    content = (final_msg.get("content") or "").strip()

    if (
        not content
        or content.lstrip().startswith("{")
        or "score_task ->" in content
        or "Theory block is not finished yet" in content
        or "Вопрос 1/" in content
        or "Сегодня мы" in content
        or content.startswith("Проверка завершена. Passrate:")
    ):
        content = _practice_fallback_feedback(state)
        backend_generated_reply = True

    score_result = state.artifacts.get("score_result") or {}
    if (
        score_result
        and score_result.get("comment")
        and (
            not content
            or content.lstrip().startswith("{")
            or "score_task ->" in content
            or "Theory block is not finished yet" in content
            or "Вопрос 1/" in content
            or "Сегодня мы" in content
            or content.startswith("Проверка завершена. Passrate:")
        )
    ):
        content = _practice_reply_from_score(state)
        backend_generated_reply = True

    db.add(models.Message(session_id=session.id, sender="model", text=content, task_id=task_id))
    db.commit()

    return {"reply": content, "tool_results": tool_results_for_ui}
