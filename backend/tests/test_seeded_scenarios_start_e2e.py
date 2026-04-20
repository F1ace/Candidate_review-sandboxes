from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import main as main_module, models
from app.database import SessionLocal
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


def _expected_first_question(task: dict) -> str:
    questions = task.get("questions") or []
    assert questions, "Seeded theory task must have at least one question"
    first_question = questions[0]
    if isinstance(first_question, dict):
        text = (
            first_question.get("text")
            or first_question.get("question")
            or first_question.get("prompt")
            or ""
        ).strip()
    else:
        text = str(first_question).strip()
    return f"**Вопрос 1/{len(questions)}:** {text}"


def _load_seeded_scenarios() -> list[models.Scenario]:
    with SessionLocal() as db:
        return db.query(models.Scenario).order_by(models.Scenario.id.asc()).all()


def test_all_seeded_scenarios_start_cleanly_in_nonstream(monkeypatch):
    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        assert "одно короткое приветствие" in system_text.lower()
        assert "объясни всё, что знаешь" not in system_text.lower()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Здравствуйте! Сейчас я подробно расскажу структуру интервью, "
                            "критерии оценки, возможные переходы между блоками и все дальнейшие шаги."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(nonstream_module.lm_client, "chat", fake_chat)

    with TestClient(main_module.app) as client:
        scenarios = _load_seeded_scenarios()
        failures: list[str] = []

        for scenario in scenarios:
            create_resp = client.post(
                "/sessions",
                json={
                    "scenario_id": scenario.id,
                    "role_id": scenario.role_id,
                },
            )
            assert create_resp.status_code == 201

            payload = create_resp.json()
            first_task_id = (scenario.tasks or [{}])[0].get("id")
            assert payload["current_task_id"] == first_task_id

            response = client.post(f"/sessions/{payload['id']}/lm/chat")
            if response.status_code != 200:
                failures.append(f"{scenario.slug}: HTTP {response.status_code}")
                continue

            final_text = response.json()["message"]["content"]
            expected_question = _expected_first_question((scenario.tasks or [])[0])

            if not final_text.startswith("Здравствуйте!"):
                failures.append(f"{scenario.slug}: нет корректного приветствия")
            if expected_question not in final_text:
                failures.append(f"{scenario.slug}: не найден первый вопрос")

        assert not failures, "\n".join(failures)


def test_all_seeded_scenarios_start_cleanly_in_streaming(monkeypatch):
    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        assert "одно короткое приветствие" in system_text.lower()
        assert "объясни всё, что знаешь" not in system_text.lower()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Привет! Сначала опишу вам весь сценарий интервью, затем расскажу про роль, "
                            "критерии оценки и только потом когда-нибудь перейду к вопросам."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    with TestClient(main_module.app) as client:
        scenarios = _load_seeded_scenarios()
        failures: list[str] = []

        for scenario in scenarios:
            create_resp = client.post(
                "/sessions",
                json={
                    "scenario_id": scenario.id,
                    "role_id": scenario.role_id,
                },
            )
            assert create_resp.status_code == 201

            payload = create_resp.json()
            first_task_id = (scenario.tasks or [{}])[0].get("id")
            assert payload["current_task_id"] == first_task_id

            response = client.get(f"/sessions/{payload['id']}/lm/chat-stream")
            if response.status_code != 200:
                failures.append(f"{scenario.slug}: HTTP {response.status_code}")
                continue

            final_text = _parse_sse_done_content(response.text)
            expected_question = _expected_first_question((scenario.tasks or [])[0])

            if not final_text.startswith("Привет!"):
                failures.append(f"{scenario.slug}: нет корректного приветствия")
            if expected_question not in final_text:
                failures.append(f"{scenario.slug}: не найден первый вопрос")

        assert not failures, "\n".join(failures)
