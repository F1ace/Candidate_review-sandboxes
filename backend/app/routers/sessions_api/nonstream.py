import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ... import models
from ...services.lm_client import lm_client
from .dispatch import _dispatch_tool_call
from .practice import _score_feedback
from .prompting import _build_system_prompt, _extract_inline_tool_call
from .state import (
    _conversation_snapshot,
    _convert_history,
    _first_practice_task,
    _get_task_by_id,
    _theory_is_complete,
    _theory_summary_text,
)
from .tools import TOOLS
def call_model(session_id: str, db: Session):
    """Non-streaming call (fallback)."""
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    history_db = (
        db.query(models.Message)
        .filter_by(session_id=session_id)
        .order_by(models.Message.created_at)
        .all()
    )
    rag_available = False
    if session.scenario.rag_corpus_id:
        rag_available = db.query(models.Document).filter_by(rag_corpus_id=session.scenario.rag_corpus_id).count() > 0
    system_prompt = _build_system_prompt(session, rag_available)
    snapshot = _conversation_snapshot(session, history_db)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": snapshot},
    ]
    messages.extend(_convert_history(history_db))

    try:
        first_resp = lm_client.chat(messages, tools=TOOLS)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"LM request failed: {exc}") from exc

    assistant_msg = first_resp["choices"][0]["message"]
    tool_calls = assistant_msg.get("tool_calls")

    # Fallback: РµСЃР»Рё tool_calls РЅРµС‚, РЅРѕ РјРѕРґРµР»СЊ РЅР°РїРµС‡Р°С‚Р°Р»Р° tool-call С‚РµРєСЃС‚РѕРј
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
            # С‡С‚РѕР±С‹ "<|channel|>commentary ..." РЅРµ РїРѕРєР°Р·С‹РІР°Р»СЃСЏ РІ UI
            assistant_msg["content"] = None

    messages.append(assistant_msg)

    tool_results_db: list[models.Message] = []
    last_score_result: dict[str, Any] | None = None
    final_msg = assistant_msg
    if tool_calls:
        tool_messages = []
        for tc in tool_calls:
            result = _dispatch_tool_call(session, tc, db)
            if tc["function"]["name"] == "score_task":
                last_score_result = result
            # --- theory->practice transition for NON-stream chat too ---
            try:
                args_sc = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args_sc = {}
            task_id_scored = args_sc.get("task_id")
            task_obj = _get_task_by_id(session.scenario, task_id_scored) if task_id_scored else None

            if task_obj and task_obj.get("type") == "theory" and _theory_is_complete(session):
                summary = _theory_summary_text(session)
                practice_task = _first_practice_task(session.scenario)

                practice_title = practice_task.get("title") if practice_task else ""
                practice_id = practice_task.get("id") if practice_task else ""
                practice_type = practice_task.get("type") if practice_task else ""
                practice_desc = ""
                if practice_task:
                    practice_desc = (
                        practice_task.get("description_for_candidate")
                        or practice_task.get("description")
                        or practice_task.get("prompt")
                        or ""
                    )

                messages.append({
                    "role": "system",
                    "content": (
                        "РўР•РћР РРЇ Р—РђР’Р•Р РЁР•РќРђ.\n"
                        "РќСѓР¶РЅРѕ: 1) РєСЂР°С‚РєРѕ СЃРѕРѕР±С‰РёС‚СЊ РёС‚РѕРі С‚РµРѕСЂРёРё, 2) РѕР±СЉСЏРІРёС‚СЊ РїРµСЂРµС…РѕРґ Рє РїСЂР°РєС‚РёРєРµ, "
                        "3) СЃРєР°Р·Р°С‚СЊ, С‡С‚Рѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЋ РЅСѓР¶РЅРѕ РїРµСЂРµР№С‚Рё РЅР° РІРєР»Р°РґРєСѓ В«РџСЂР°РєС‚РёРєР°В», РІСЃС‚Р°РІРёС‚СЊ СЂРµС€РµРЅРёРµ РІ СЂРµРґР°РєС‚РѕСЂ Рё РЅР°Р¶Р°С‚СЊ В«РџСЂРѕРІРµСЂРёС‚СЊ РјРѕРґРµР»СЊСЋВ», "
                        "4) РЅР°Р·РІР°С‚СЊ СЃР»РµРґСѓСЋС‰РµРµ РїСЂР°РєС‚РёС‡РµСЃРєРѕРµ Р·Р°РґР°РЅРёРµ.\n\n"
                        f"{summary}\n"
                        f"РЎР»РµРґСѓСЋС‰РµРµ РїСЂР°РєС‚РёС‡РµСЃРєРѕРµ Р·Р°РґР°РЅРёРµ: {practice_id} {practice_title} (С‚РёРї: {practice_type}).\n"
                        f"РћРїРёСЃР°РЅРёРµ: {practice_desc}\n"
                    )
                })
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            try:
                args = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                args = {}
            task_id_for_db = args.get("task_id")

            tool_results_db.append(
                models.Message(
                    session_id=session_id,
                    sender="tool",
                    text=f"{tc['function']['name']} -> {result}",
                    task_id=task_id_for_db,
                )
            )

        messages.extend(tool_messages)
        try:
            second_resp = lm_client.chat(messages, tools=TOOLS)
            final_msg = second_resp["choices"][0]["message"]
            # --- Fallback 2: РјРѕРґРµР»СЊ РјРѕРіР»Р° "РЅР°РїРµС‡Р°С‚Р°С‚СЊ" tool-call С‚РµРєСЃС‚РѕРј РІРѕ РІС‚РѕСЂРѕРј РѕС‚РІРµС‚Рµ ---
            if not (final_msg.get("tool_calls") or []):
                inline = _extract_inline_tool_call(final_msg.get("content") or "")
                if inline:
                    tool_name, args = inline
                    # РІС‹РїРѕР»РЅРёРј tool РІСЂСѓС‡РЅСѓСЋ, РєР°Рє РµСЃР»Рё Р±С‹ СЌС‚Рѕ Р±С‹Р» tool_call
                    fake_tc = {
                        "id": "inline_toolcall_2",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                    result = _dispatch_tool_call(session, fake_tc, db)

                    # Р·Р°Р»РѕРіРёСЂСѓРµРј tool РІ Р‘Р” (РєР°Рє С‚С‹ РґРµР»Р°РµС€СЊ РІС‹С€Рµ РґР»СЏ tool_calls)
                    db.add(models.Message(
                        session_id=session_id,
                        sender="tool",
                        text=f"{tool_name} -> {result}",
                        task_id=args.get("task_id"),
                    ))
                    db.commit()

                    # РґРѕР±Р°РІРёРј tool-РѕС‚РІРµС‚ РІ messages Рё СЃРїСЂРѕСЃРёРј РјРѕРґРµР»СЊ РµС‰С‘ СЂР°Р·
                    messages.append({"role": "assistant", "content": None})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": "inline_toolcall_2",
                        "content": json.dumps(result, ensure_ascii=False),
                    })

                    third_resp = lm_client.chat(messages, tools=TOOLS)
                    final_msg = third_resp["choices"][0]["message"]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"LM request failed after tool calls: {exc}") from exc
        if (not final_msg.get("content")) and last_score_result:
            final_msg["content"] = _score_feedback(last_score_result)

    for tm in tool_results_db:
        db.add(tm)
    db.add(
        models.Message(
            session_id=session_id,
            sender="model",
            text=final_msg.get("content") or "",
        )
    )
    db.commit()

    return {"message": final_msg}

