from __future__ import annotations

import json

from app import models
from app.routers.sessions_api import nonstream as nonstream_module


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


def test_nonstream_theory_flow_uses_rag_for_validation(
    client,
    db_session,
    theory_scenario_factory,
    embeddings_backend,
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

    call_index = {"value": 0}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        call_index["value"] += 1
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")

        if call_index["value"] == 1:
            assert tools is not None
            assert "<THEORY_RAG_EVIDENCE>" in system_text
            args = {
                "task_id": "T-DOCS",
                "points": 8.0,
                "comment": (
                    "Ответ подтверждается документами сценария и корректно объясняет базовый смысл "
                    "идемпотентности. Связь с POST указана верно, хотя формулировку можно сделать точнее."
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
                                    "id": "score_task_intermediate",
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

        if call_index["value"] == 2:
            assert tools is not None
            assert "Все промежуточные оценки theory-блока уже сохранены" in system_text
            args = {
                "task_id": "T-DOCS",
                "points": 8.0,
                "comment": (
                    "Теоретический блок завершён уверенно: кандидат верно описал идемпотентность "
                    "и не ошибся в трактовке POST. Для максимального балла не хватило чуть более "
                    "строгого различения свойства операции и поведения конкретного API."
                ),
                "is_final": True,
                "question_index": None,
            }
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "score_task_final",
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

        assert tools is None
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Теоретический этап завершён. По документам сценария ответ подтверждается. "
                            "Итоговая оценка: 8/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(nonstream_module.lm_client, "chat", fake_chat)

    response = client.post(f"/sessions/{session.id}/lm/chat")
    assert response.status_code == 200
    payload = response.json()
    final_text = payload["message"]["content"]
    assert "Итоговая оценка: 8/10" in final_text
    assert "подтверждается" in final_text

    validations = (
        db_session.query(models.TheoryFactValidation)
        .filter_by(session_id=session.id, task_id="T-DOCS", question_index=1)
        .all()
    )
    assert len(validations) == 1
    assert validations[0].result_count >= 1
    assert "POST" in json.dumps(validations[0].evidence, ensure_ascii=False)
    assert validations[0].evidence[0]["metadata"]["retrieval_backend"] == "langchain_inmemory_vectorstore"
    assert validations[0].evidence[0]["metadata"]["embedding_model"] == "fake-lmstudio-embedding"
    assert embeddings_backend.document_calls
    assert embeddings_backend.query_calls

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
    assert session.scores["T-DOCS"] == 8.0
