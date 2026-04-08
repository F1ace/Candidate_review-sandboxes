import re
from typing import Any

from sqlalchemy.orm import Session

from ... import models
from ...services.lm_client import lm_client
from ...services.practice.code_orchestrator import run_practice_code_review
from ...services.practice.sql_orchestrator import run_practice_sql_review
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

def _practice_sql_agent_review(
    *,
    session: models.Session,
    db: Session,
    instruction: str,
    task_id: str,
    candidate_query: str,
    max_iters: int = 8,
) -> dict[str, Any]:
    return run_practice_sql_review(
        session=session,
        db=db,
        instruction=instruction,
        task_id=task_id,
        candidate_query=candidate_query,
        tools=TOOLS,
        chat=lm_client.chat,
        build_system_prompt=_build_system_prompt,
        conversation_snapshot=_conversation_snapshot,
        extract_inline_tool_call=_extract_inline_tool_call,
        dispatch_tool_call=_dispatch_tool_call,
        get_task_by_id=_get_task_by_id,
        logger=logger,
        max_iters=max_iters,
    )

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

def _score_feedback(result: dict[str, Any], theory_max_points: int = 10):
    if not isinstance(result, dict):
        result = {}

    task_id = result.get("task_id") or ""
    pts = result.get("points")
    pts_txt = f"{int(pts)}/{theory_max_points}" if pts is not None else "оценка выставлена"
    comment = (result.get("comment") or "").strip()
    aggregated = result.get("aggregated") or {}
    aggregated_comments = aggregated.get("comments") or []
    points = result.get("points")
    aggregated = result.get("aggregated") or {}
    if aggregated.get("avg_points") is not None:
        points = aggregated.get("avg_points")

    comment = (result.get("comment") or "").strip()

    if not isinstance(aggregated_comments, list):
        aggregated_comments = []

    aggregated_comments = [str(x).strip() for x in aggregated_comments if str(x).strip()]

    question_breakdown_lines: list[str] = []
    for idx, item in enumerate(aggregated_comments):
        text = str(item).strip()
        if not text:
            continue
        question_breakdown_lines.append(f"- Вопрос {idx + 1}: {text}")

    question_breakdown_block = "\n".join(question_breakdown_lines)

    raw_is_final = result.get("is_final", True)
    is_final = raw_is_final if isinstance(raw_is_final, bool) else str(raw_is_final).lower() == "true"

    if is_final and aggregated_comments:
        comment = ""

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

    if question_breakdown_block:
        parts.append(question_breakdown_block)
    elif comment:
        parts.append(comment)
    else:
        parts.append("Ответы по теоретическому блоку проверены, итоговая оценка выставлена.")

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
