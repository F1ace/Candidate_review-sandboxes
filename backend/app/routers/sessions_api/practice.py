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

def _score_feedback(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        result = {}

    task_id = result.get("task_id") or ""
    pts = result.get("points")
    pts_txt = f"{int(pts)}/10" if pts is not None else "оценка выставлена"
    comment = (result.get("comment") or "").strip()
    is_final = result.get("is_final", True) is True

    if not is_final:
        # Промежуточный score_task по теории не должен показываться как итог.
        return ""

    # Пытаемся аккуратно выделить зоны роста из комментария.
    growth_points: list[str] = []
    strengths_text = comment

    split_markers = [
        "что можно улучшить",
        "что стоило бы добавить",
        "можно было бы добавить",
        "можно было бы усилить",
        "зоны роста",
        "для усиления ответа",
        "для более сильного ответа",
    ]

    lowered = comment.lower()
    for marker in split_markers:
        idx = lowered.find(marker)
        if idx != -1:
            strengths_text = comment[:idx].strip(" \n:-")
            tail = comment[idx:].strip()
            growth_points.append(tail)
            break

    # Если явного разделения нет — даём нейтральную зону роста.
    if not growth_points:
        growth_points.append(
            "Для усиления ответа стоит добавлять больше конкретных продуктовых примеров, "
            "явнее проговаривать trade-off'ы и чуть подробнее раскрывать практическую интерпретацию метрик и результатов эксперимента."
        )

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

    if strengths_text:
        parts.append(strengths_text)
    else:
        parts.append(
            "Ответы в целом показали понимание ключевых концепций блока и базовую уверенность в теории экспериментов."
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

