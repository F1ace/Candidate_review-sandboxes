from __future__ import annotations

import json

from app import models
from app.routers.sessions_api import nonstream as nonstream_module
from app.routers.sessions_api import prompting as prompting_module
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
    scenario_name: str = "Theory greeting",
    question_text: str = "Что такое идемпотентность?",
) -> models.Session:
    role = models.Role(name="Backend", slug="backend-greeting", description="role")
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


def _complete_opening(session: models.Session) -> str:
    return (
        f'Привет! Я проведу интервью на роль {session.role.name} по сценарию "{session.scenario.name}". '
        "Цель интервью — понять, как вы рассуждаете и отвечаете на вопросы этого сценария.\n\n"
        "**Вопрос 1/1:** Что такое идемпотентность?"
    )


def test_build_theory_question_message_uses_utf8_question_text(db_session):
    session = _create_theory_session(
        db_session,
        question_text="Что такое переобучение?",
    )

    message = streaming_module._build_theory_question_message(session, "T1", 1)

    assert message == "**Вопрос 1/1:** Что такое переобучение?"


def test_first_model_greeting_preserves_model_text_without_backend_injection(db_session):
    session = _create_theory_session(db_session, scenario_name="Theory greeting preserved")

    result = prompting_module._ensure_first_model_greeting(
        'Привет! Я проведу интервью на роль Backend по сценарию "Theory greeting preserved". '
        "Цель интервью — коротко обозначить контекст и сразу перейти к вопросу.",
        session,
    )

    assert result.startswith("Привет!")
    assert result.count("Привет!") == 1
    assert "Здравствуйте! Проведу для вас интервью" not in result


def test_streaming_first_message_is_repaired_by_model_with_role_scenario_and_goal(client, db_session, monkeypatch):
    session = _create_theory_session(db_session, scenario_name="Theory streaming")
    calls = 0

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        nonlocal calls
        calls += 1
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        assert tools is None
        if calls == 1:
            assert "роль, сценарий и цель интервью" in system_text.lower()
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Привет! Рада встрече.\n\n**Вопрос 1/1:** Что такое идемпотентность?",
                        }
                    }
                ]
            }

        assert "цель интервью" in system_text.lower()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": _complete_opening(session),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200

    final_text = _parse_sse_done_content(response.text)
    assert calls == 2
    assert final_text.startswith("Привет!")
    assert session.role.name in final_text
    assert session.scenario.name in final_text
    assert "Цель интервью" in final_text
    assert "**Вопрос 1/1:** Что такое идемпотентность?" in final_text
    assert "Здравствуйте! Проведу для вас интервью" not in final_text

    db_session.expire_all()
    saved_messages = (
        db_session.query(models.Message)
        .filter_by(session_id=session.id, sender="model")
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )
    assert saved_messages
    assert saved_messages[-1].text == final_text


def test_nonstream_first_message_is_repaired_by_model_with_role_scenario_and_goal(client, db_session, monkeypatch):
    session = _create_theory_session(db_session, scenario_name="Theory nonstream")
    calls = 0

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        nonlocal calls
        calls += 1
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        assert tools is None
        if calls == 1:
            assert "роль, сценарий и цель интервью" in system_text.lower()
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Привет! Рада встрече.\n\n**Вопрос 1/1:** Что такое идемпотентность?",
                        }
                    }
                ]
            }

        assert "цель интервью" in system_text.lower()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": _complete_opening(session),
                    }
                }
            ]
        }

    monkeypatch.setattr(nonstream_module.lm_client, "chat", fake_chat)

    response = client.post(f"/sessions/{session.id}/lm/chat")
    assert response.status_code == 200

    payload = response.json()
    final_text = payload["message"]["content"]
    assert calls == 2
    assert final_text.startswith("Привет!")
    assert session.role.name in final_text
    assert session.scenario.name in final_text
    assert "Цель интервью" in final_text
    assert "**Вопрос 1/1:** Что такое идемпотентность?" in final_text
    assert "Здравствуйте! Проведу для вас интервью" not in final_text

    db_session.expire_all()
    saved_message = (
        db_session.query(models.Message)
        .filter_by(session_id=session.id, sender="model")
        .order_by(models.Message.created_at.desc(), models.Message.id.desc())
        .first()
    )
    assert saved_message is not None
    assert saved_message.text == final_text
