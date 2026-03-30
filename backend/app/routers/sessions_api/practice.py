import re
from typing import Any

from sqlalchemy.orm import Session

from ... import models
from ...services.lm_client import lm_client
from ...services.practice.code_orchestrator import run_practice_code_review
from .dispatch import _dispatch_tool_call
from .prompting import _build_system_prompt, _extract_inline_tool_call
from .router import logger
from .state import _conversation_snapshot, _convert_history, _get_task_by_id
from .tools import TOOLS

def _practice_agent_review(
    *,
    session: models.Session,
    db: Session,
    instruction: str,
    task_id: str,
) -> dict[str, Any]:
    return run_practice_code_review(
        session=session,
        db=db,
        instruction=instruction,
        task_id=task_id,
        tools=TOOLS,
        chat=lm_client.chat,
        build_system_prompt=_build_system_prompt,
        conversation_snapshot=_conversation_snapshot,
        convert_history=_convert_history,
        extract_inline_tool_call=_extract_inline_tool_call,
        dispatch_tool_call=_dispatch_tool_call,
        get_task_by_id=_get_task_by_id,
        logger=logger,
    )

def _sql_practice_reply_from_score(
    score_result: dict[str, Any] | None,
    *,
    max_points: int,
    fallback_reply: str = "",
) -> str:
    score_result = score_result or {}

    points = score_result.get("points")
    comment = (score_result.get("comment") or "").strip()

    if points is None and fallback_reply.strip():
        return fallback_reply.strip()

    if points is None:
        score_line = f"Оценка: не выставлена из {max_points}"
    else:
        score_line = f"Оценка: {int(round(float(points)))}/{max_points}"

    parts: list[str] = [score_line]

    if comment:
        parts.extend(["", comment])
    elif fallback_reply.strip():
        parts.extend(["", fallback_reply.strip()])

    return "\n".join(parts).strip()

def _practice_sql_agent_review(
    *,
    session: models.Session,
    db: Session,
    instruction: str,
    task_id: str,
    max_iters: int = 8,
) -> dict[str, Any]:
    def _has_sql_sections(text: str) -> bool:
        required_headers = [
            "Корректность:",
            "Качество решения:",
            "Работа с SQL:",
            "Что можно улучшить:",
        ]
        return all(h in (text or "") for h in required_headers)

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
            .count() > 0
        )

    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
        {
            "role": "system",
            "content": (
                "PRACTICE_MODE_SQL.\n"
                "Сейчас идет ТОЛЬКО проверка SQL-задания.\n"
                "Не анализируй Python-код и не ищи candidate_code.\n"
                "Разрешённые инструменты: только run_sql и score_task.\n\n"
                "Порядок действий обязателен:\n"
                "1. Сначала вызови run_sql.\n"
                "2. Проанализируй результат выполнения SQL.\n"
                "3. Затем вызови score_task.\n"
                "4. Только после успешного score_task дай финальный ответ кандидату.\n\n"
                "И comment в score_task, и финальный ответ кандидату должны содержать РОВНО 4 непустые секции:\n"
                "Корректность: ...\n"
                "Качество решения: ...\n"
                "Работа с SQL: ...\n"
                "Что можно улучшить: ...\n\n"
                "Не используй заголовки из кодового шаблона.\n"
                "Не выводи JSON, tool dump или служебные пояснения."
            ),
        },
        {"role": "user", "content": instruction},
    ]

    final_msg: dict[str, Any] | None = None
    tool_results_for_ui: list[dict[str, Any]] = []
    run_sql_seen = False
    score_saved = False
    last_score_result: dict[str, Any] | None = None
    task_obj = _get_task_by_id(session.scenario, task_id) or {}
    max_points = int(task_obj.get("max_points", 0) or 0)

    allowed_tools = [
        t for t in TOOLS
        if (t.get("function") or {}).get("name") in {"run_sql", "score_task"}
    ]

    for _ in range(max_iters):
        resp = lm_client.chat(messages, tools=allowed_tools)
        assistant_msg = resp["choices"][0]["message"]

        tool_calls = assistant_msg.get("tool_calls") or []
        if tool_calls:
            messages.append(assistant_msg)

            for tc in tool_calls:
                tool_result = _dispatch_tool_call(session, tc, db)
                tool_name = ((tc.get("function") or {}).get("name") or "").split(".")[-1]

                # Явно логируем tool-вызов в messages, чтобы он сохранялся в БД
                tool_msg = models.Message(
                    session_id=session.id,
                    sender="tool",
                    text=f"{tool_name} -> {tool_result}",
                    task_id=task_id,
                )
                db.add(tool_msg)
                db.commit()

                tool_results_for_ui.append(
                    {
                        "tool": tool_name,
                        "result": tool_result,
                    }
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": str(tool_result),
                    }
                )

                if tool_name == "run_sql":
                    if isinstance(tool_result, dict) and tool_result.get("ok"):
                        run_sql_seen = True
                    else:
                        err = ""
                        if isinstance(tool_result, dict):
                            err = str(tool_result.get("error") or "")
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Вызов run_sql не прошёл.\n"
                                    f"Ошибка: {err}\n"
                                    "Повтори run_sql корректно, используя task_id и query."
                                ),
                            }
                        )

                if tool_name == "score_task":
                    if isinstance(tool_result, dict) and tool_result.get("ok"):
                        score_saved = True
                        last_score_result = tool_result
                    else:
                        err = ""
                        if isinstance(tool_result, dict):
                            err = str(tool_result.get("error") or "")
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "Предыдущий вызов score_task не прошёл.\n"
                                    f"Ошибка: {err}\n\n"
                                    "Повтори score_task ещё раз.\n"
                                    "Точный шаблон comment:\n"
                                    "Корректность: ...\n"
                                    "Качество решения: ...\n"
                                    "Работа с SQL: ...\n"
                                    "Что можно улучшить: ...\n"
                                    "Все секции должны быть непустыми."
                                ),
                            }
                        )
            continue

        content = (assistant_msg.get("content") or "").strip()

        inline = _extract_inline_tool_call(content)
        if inline:
            tool_name, args = inline
            fake_tc = {
                "id": "inline-sql-tool-call",
                "function": {
                    "name": tool_name,
                    "arguments": __import__("json").dumps(args, ensure_ascii=False),
                }
            }

            tool_result = _dispatch_tool_call(session, fake_tc, db)

            tool_msg = models.Message(
                session_id=session.id,
                sender="tool",
                text=f"{tool_name} -> {tool_result}",
                task_id=task_id,
            )
            db.add(tool_msg)
            db.commit()

            tool_results_for_ui.append(
                {
                    "tool": tool_name,
                    "result": tool_result,
                }
            )

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "content": str(tool_result),
                }
            )

            if tool_name == "run_sql":
                if isinstance(tool_result, dict) and tool_result.get("ok"):
                    run_sql_seen = True

            if tool_name == "score_task":
                if isinstance(tool_result, dict) and tool_result.get("ok"):
                    score_saved = True
                    last_score_result = tool_result
            continue

        if not run_sql_seen:
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                }
            )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Ты ещё не выполнил обязательный вызов run_sql.\n"
                        "Сначала вызови run_sql с task_id и query."
                    ),
                }
            )
            continue

        if not score_saved:
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                }
            )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Ты ещё не выполнил обязательный успешный вызов score_task.\n"
                        "Сначала вызови score_task, потом дай финальный ответ."
                    ),
                }
            )
            continue

        if not _has_sql_sections(content):
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                }
            )
            exact_points = None
            if isinstance(last_score_result, dict):
                exact_points = last_score_result.get("points")

            points_hint = (
                f"Оценка: {int(round(float(exact_points)))}/{max_points}"
                if exact_points is not None
                else f"Оценка: [балл из score_task]/{max_points}"
            )

            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Финальный ответ должен содержать:\n"
                        f"- первую строку СТРОГО в формате: {points_hint}\n"
                        "- затем РОВНО 4 непустые секции:\n"
                        "Корректность: ...\n"
                        "Качество решения: ...\n"
                        "Работа с SQL: ...\n"
                        "Что можно улучшить: ...\n"
                        "Нельзя менять названия секций.\n"
                        "Нельзя выводить JSON, tool dump или служебный текст."
                    ),
                }
            )
            continue

        final_msg = assistant_msg
        break

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
        reply = _sql_practice_reply_from_score(
            last_score_result,
            max_points=max_points,
            fallback_reply=reply or fallback_reply,
        )
    elif not reply:
        reply = fallback_reply
        
    return {
        "reply": reply,
        "tool_results": tool_results_for_ui,
    }

def _build_dynamic_growth_points(result: dict[str, Any]) -> list[str]:
    aggregated = result.get("aggregated") or {}
    comments = aggregated.get("comments") or []

    if not isinstance(comments, list):
        comments = []

    comments = [str(c).strip() for c in comments if str(c).strip()]
    if not comments:
        return [
            "Для усиления ответа стоит добавлять больше конкретных продуктовых примеров и чуть подробнее раскрывать практическую интерпретацию результатов эксперимента."
        ]

    text = " ".join(comments).lower()

    growth_points: list[str] = []

    def has_any(*phrases: str) -> bool:
        return any(p.lower() in text for p in phrases)

    if has_any(
        "не упоминает конкретные примеры",
        "не приводит конкретный пример",
        "без конкретных примеров",
        "можно добавить пример",
        "стоило бы добавить пример",
    ):
        growth_points.append(
            "Добавляйте больше конкретных продуктовых примеров: как именно метрика выбирается в A/B-тесте, какие guardrail-метрики важны и как решение влияет на продукт."
        )

    if has_any(
        "упущены детали",
        "не раскрыты детали",
        "не хватает деталей",
        "можно подробнее",
        "стоит подробнее",
        "раскрыто не полностью",
    ):
        growth_points.append(
            "Старайтесь глубже раскрывать детали ответа: не только давать определение, но и пояснять механику, ограничения метода и типичные ошибки интерпретации."
        )

    if has_any(
        "интерпретац",
        "практическ",
        "ошибках при интерпретации",
        "порог p-value",
        "не объясняет",
    ):
        growth_points.append(
            "Усильте практическую интерпретацию: что означает метрика или статистический результат для бизнеса, какие выводы можно сделать и какие решения принимать дальше."
        )

    if has_any(
        "не упоминает порог",
        "не приводит порог",
        "не указан порог",
        "хи-квадрат",
        "проверки",
    ):
        growth_points.append(
            "В вопросах про эксперименты полезно точнее проговаривать критерии проверки гипотез: какой тест используется, какой порог значимости берётся и как интерпретировать результат проверки."
        )

    if has_any(
        "не полностью раскрывает",
        "можно усилить",
        "можно было бы добавить",
        "не охватывает",
    ):
        growth_points.append(
            "Старайтесь структурировать ответ по схеме: определение → зачем метод нужен → как применяется на практике → ограничения и риски."
        )

    # Убираем дубли
    unique_growth_points: list[str] = []
    seen = set()
    for item in growth_points:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_growth_points.append(item)

    if unique_growth_points:
        return unique_growth_points[:3]

    # Фолбэк: пытаемся извлечь хвосты после маркеров из comments
    extracted: list[str] = []
    patterns = [
        r"(?:можно было бы добавить[^.?!]*[.?!])",
        r"(?:стоило бы добавить[^.?!]*[.?!])",
        r"(?:не хватает[^.?!]*[.?!])",
        r"(?:упущены[^.?!]*[.?!])",
        r"(?:не раскрыты[^.?!]*[.?!])",
    ]

    for comment in comments:
        for pattern in patterns:
            for m in re.findall(pattern, comment, flags=re.IGNORECASE):
                cleaned = m.strip()
                if cleaned:
                    extracted.append(cleaned)

    # Убираем дубли
    unique_extracted: list[str] = []
    seen = set()
    for item in extracted:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_extracted.append(item)

    if unique_extracted:
        return unique_extracted[:3]

    return [
        "Для усиления ответа полезно чаще связывать теорию с продуктовой практикой: приводить примеры, обозначать trade-off'ы и объяснять, как выводы из эксперимента влияют на решение команды."
    ]

def _score_feedback(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        result = {}

    task_id = result.get("task_id") or ""
    pts = result.get("points")
    pts_txt = f"{int(pts)}/10" if pts is not None else "оценка выставлена"
    comment = (result.get("comment") or "").strip()

    raw_is_final = result.get("is_final", True)
    is_final = raw_is_final if isinstance(raw_is_final, bool) else str(raw_is_final).lower() == "true"

    if not is_final:
        return ""

    growth_points = _build_dynamic_growth_points(result)

    parts = [
        "Теоретический этап завершён.",
        "",
        "**1) Блок с оценкой**",
        f"- Итоговая оценка за теоретический блок: **{pts_txt}**.",
    ]

    if task_id:
        parts.append(f"- Идентификатор блока: `{task_id}`.")

    parts.extend([
        "",
        "**2) Блок с комментарием по содержанию ответа**",
    ])

    if comment:
        parts.append(comment)
    else:
        parts.append(
            "Ответы в целом показали понимание ключевых концепций теоретического блока."
        )

    parts.extend([
        "",
        "**3) Блок с зонами роста**",
    ])

    for item in growth_points:
        parts.append(f"- {item}")

    parts.extend([
        "",
        "**Что дальше**",
        "Интервью продолжается в блоке с практическим заданием.",
    ])

    return "\n".join(parts)
