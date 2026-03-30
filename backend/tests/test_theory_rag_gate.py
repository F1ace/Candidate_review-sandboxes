from __future__ import annotations

from app import models


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


def test_direct_theory_score_requires_rag_validation(client, db_session, theory_scenario_factory):
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
            text="Идемпотентность значит, что повтор запроса ничего не меняет; POST обычно не идемпотентен.",
            task_id="T-DOCS",
        )
    )
    db_session.commit()

    response = client.post(
        f"/sessions/{session.id}/score",
        json={
            "task_id": "T-DOCS",
            "points": 7,
            "comment": (
                "Ответ в целом отражает базовую идею идемпотентности и корректно отмечает, "
                "что POST обычно не считается идемпотентным по умолчанию."
            ),
            "is_final": False,
            "question_index": 1,
        },
    )

    assert response.status_code == 400
    assert "validated against scenario documents" in response.text
