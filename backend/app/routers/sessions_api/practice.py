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

    return (
        f"Теоретический этап завершён.\n\n"
        f"**Оценка:** {pts_txt} за блок {task_id}.\n\n"
        f"Продолжение интервью будет происходить во вкладке практического задания."
    )

