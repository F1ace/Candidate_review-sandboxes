from __future__ import annotations

import json

from app import models
from app.routers.sessions_api import nonstream as nonstream_module
from app.routers.sessions_api import streaming as streaming_module


def _parse_sse_done_content(raw_text: str) -> str:
    done_payload = ""
    for part in raw_text.split("\n\n"):
        if not part.strip().startswith("data:"):
            continue
        payload = json.loads(part.strip()[5:].strip())
        if payload.get("type") == "done":
            done_payload = payload.get("content") or ""
    return done_payload


def _create_theory_session(
    db_session,
    *,
    scenario_name: str,
    question_text: str = "Что такое идемпотентность?",
) -> models.Session:
    role = models.Role(name="Backend", slug=f"{scenario_name.lower().replace(' ', '-')}-role", description="role")
    db_session.add(role)
    db_session.flush()

    scenario = models.Scenario(
        role_id=role.id,
        name=scenario_name,
        slug=f"{role.slug}-scenario",
        description="scenario",
        difficulty="middle",
        tasks=[
            {
                "id": "T1",
                "type": "theory",
                "title": "Theory",
                "max_points": 10,
                "questions": [question_text],
            }
        ],
        config={},
    )
    db_session.add(scenario)
    db_session.flush()

    session = models.Session(
        scenario_id=scenario.id,
        role_id=role.id,
        state="active",
        current_task_id="T1",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def _unexpected_tool_response(tools):
    if tools:
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "score_task",
                                    "arguments": json.dumps(
                                        {
                                            "task_id": "T1",
                                            "points": 7,
                                            "comment": "Промежуточная оценка.",
                                            "question_index": 1,
                                            "is_final": False,
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    return None


def _repaired_opening(session: models.Session) -> str:
    return (
        f'Привет! Я проведу интервью на роль {session.role.name} по сценарию "{session.scenario.name}". '
        "Цель интервью — проверить ваши знания по текущему сценарию.\n\n"
        "**Вопрос 1/1:** Что такое идемпотентность?"
    )


def test_nonstream_first_message_does_not_expose_score_task_before_first_question(client, db_session, monkeypatch):
    session = _create_theory_session(db_session, scenario_name="Theory first turn no tools nonstream")
    seen_tools = []
    calls = 0

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        nonlocal calls
        calls += 1
        seen_tools.append(tools)
        bad = _unexpected_tool_response(tools)
        if bad is not None:
            return bad
        if calls == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Привет! Рада встрече.",
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": _repaired_opening(session),
                    }
                }
            ]
        }

    monkeypatch.setattr(nonstream_module.lm_client, "chat", fake_chat)

    response = client.post(f"/sessions/{session.id}/lm/chat")
    assert response.status_code == 200

    payload = response.json()
    final_text = payload["message"]["content"]
    assert final_text.startswith("Привет!")
    assert session.role.name in final_text
    assert session.scenario.name in final_text
    assert "Цель интервью" in final_text
    assert "Не удалось автоматически сформировать итоговую оценку" not in final_text
    assert "**Вопрос 1/1:** Что такое идемпотентность?" in final_text
    assert seen_tools
    assert all(item is None for item in seen_tools)

    db_session.expire_all()
    tool_messages = (
        db_session.query(models.Message)
        .filter_by(session_id=session.id, sender="tool")
        .all()
    )
    assert tool_messages == []


def test_streaming_first_message_does_not_expose_score_task_before_first_question(client, db_session, monkeypatch):
    session = _create_theory_session(db_session, scenario_name="Theory first turn no tools streaming")
    seen_tools = []
    calls = 0

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        nonlocal calls
        calls += 1
        seen_tools.append(tools)
        bad = _unexpected_tool_response(tools)
        if bad is not None:
            return bad
        if calls == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Привет! Рада встрече.",
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": _repaired_opening(session),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200

    final_text = _parse_sse_done_content(response.text)
    assert final_text.startswith("Привет!")
    assert session.role.name in final_text
    assert session.scenario.name in final_text
    assert "Цель интервью" in final_text
    assert "Не удалось автоматически сформировать итоговую оценку" not in final_text
    assert "**Вопрос 1/1:** Что такое идемпотентность?" in final_text
    assert seen_tools
    assert all(item is None for item in seen_tools)

    db_session.expire_all()
    tool_messages = (
        db_session.query(models.Message)
        .filter_by(session_id=session.id, sender="tool")
        .all()
    )
    assert tool_messages == []
