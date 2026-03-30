from __future__ import annotations

import json

from app import models
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


def _create_corpus_with_document(client) -> int:
    corpus_resp = client.post("/rag/corpora", json={"name": "Theory docs", "description": "docs"})
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    upload_resp = client.post(
        f"/rag/corpora/{corpus_id}/documents/upload",
        files={
            "file": (
                "theory.txt",
                (
                    "Идемпотентность означает, что повторный вызов операции с теми же данными "
                    "не меняет результат после первого применения. POST обычно не считается идемпотентным."
                ).encode("utf-8"),
                "text/plain",
            )
        },
    )
    assert upload_resp.status_code == 201
    return corpus_id


def test_streaming_theory_flow_uses_rag_for_validation(
    client,
    db_session,
    theory_scenario_factory,
    monkeypatch,
):
    corpus_id = _create_corpus_with_document(client)
    scenario = theory_scenario_factory(rag_corpus_id=corpus_id)

    session = models.Session(
        scenario_id=scenario.id,
        role_id=scenario.role_id,
        state="active",
        current_task_id="T-DOCS",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    db_session.add(
        models.Message(
            session_id=session.id,
            sender="model",
            text="**Вопрос 1/1:** Что такое идемпотентность и как она связана с POST?",
            task_id="T-DOCS",
        )
    )
    db_session.add(
        models.Message(
            session_id=session.id,
            sender="candidate",
            text=(
                "Идемпотентность означает, что повтор операции с теми же входными данными "
                "не меняет итог после первого успешного применения. POST обычно не идемпотентен."
            ),
            task_id="T-DOCS",
        )
    )
    db_session.commit()

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")

        if tools:
            assert any((tool.get("function") or {}).get("name") == "score_task" for tool in tools)
            if "Все промежуточные оценки theory-блока уже сохранены" in system_text:
                args = {
                    "task_id": "T-DOCS",
                    "points": 7.0,
                    "comment": (
                        "Ответ корректно отражает базовую идею идемпотентности и верно связывает её "
                        "с тем, что POST по умолчанию не считается идемпотентным. При этом объяснение "
                        "можно было бы сделать немного подробнее и аккуратнее развести семантику метода и реализацию."
                    ),
                    "is_final": True,
                    "question_index": None,
                }
            else:
                assert "<THEORY_RAG_EVIDENCE>" in system_text
                args = {
                    "task_id": "T-DOCS",
                    "points": 7.0,
                    "comment": (
                        "Ответ в целом подтверждается документами сценария: кандидат правильно описал "
                        "смысл идемпотентности и отметил, что POST обычно не является идемпотентным. "
                        "Не хватает только чуть более точного разграничения между свойством операции и частными реализациями API."
                    ),
                    "is_final": False,
                    "question_index": 1,
                }
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "score_task_call",
                                    "type": "function",
                                    "function": {
                                        "name": "score_task",
                                        "arguments": json.dumps(args, ensure_ascii=False),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Теоретический этап завершён. По документам сценария ответ в целом подтверждается. "
                            "Итоговая оценка: 7/10. Дальше можно переходить к практической части."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)
    assert "Итоговая оценка: 7/10" in final_text
    assert "подтверждается" in final_text

    validations = (
        db_session.query(models.TheoryFactValidation)
        .filter_by(session_id=session.id, task_id="T-DOCS", question_index=1)
        .all()
    )
    assert len(validations) == 1
    assert validations[0].result_count >= 1
    assert "POST" in json.dumps(validations[0].evidence, ensure_ascii=False)

    scores = (
        db_session.query(models.Score)
        .filter_by(session_id=session.id, task_id="T-DOCS")
        .order_by(models.Score.created_at.asc(), models.Score.id.asc())
        .all()
    )
    assert len(scores) == 2
    assert scores[0].is_final is False
    assert scores[1].is_final is True

    db_session.refresh(session)
    assert session.scores["T-DOCS"] == 7.0
