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


def test_direct_final_theory_score_requires_comments_and_preserves_points(
    client,
    db_session,
    theory_scenario_factory,
):
    scenario = theory_scenario_factory()

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
    db_session.add(
        models.Score(
            session_id=session.id,
            task_id="T-DOCS",
            points=6.0,
            comment=(
                "Ответ в целом верный и покрывает базовую идею идемпотентности. "
                "Не хватает более точного объяснения связи с HTTP-семантикой POST."
            ),
            is_final=False,
            question_index=1,
            score_type="theory_intermediate",
        )
    )
    db_session.commit()

    missing_comments = client.post(
        f"/sessions/{session.id}/score",
        json={
            "task_id": "T-DOCS",
            "points": 4,
            "comment": (
                "Кандидат понимает базовую идею темы и не путает обычную семантику POST. "
                "При этом ответу не хватило технической точности и явного разведения уровней абстракции."
            ),
            "is_final": True,
            "question_index": None,
        },
    )
    assert missing_comments.status_code == 400
    assert "Final theory comments are required" in missing_comments.text

    response = client.post(
        f"/sessions/{session.id}/score",
        json={
            "task_id": "T-DOCS",
            "points": 4,
            "comment": (
                "Кандидат понимает базовую идею темы и не путает обычную семантику POST. "
                "При этом ответу не хватило технической точности и явного разведения уровней абстракции."
            ),
            "comments": [
                "Кандидат верно описал смысл идемпотентности и корректно связал его с POST, но объяснение осталось слишком общим."
            ],
            "is_final": True,
            "question_index": None,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["points"] == 4.0
    assert payload["comments"] == [
        "Кандидат верно описал смысл идемпотентности и корректно связал его с POST, но объяснение осталось слишком общим."
    ]

    db_session.refresh(session)
    assert session.scores["T-DOCS"] == 4.0


def test_direct_intermediate_theory_score_infers_non_final_when_flag_missing(
    client,
    db_session,
    theory_scenario_factory,
):
    scenario = theory_scenario_factory()

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

    response = client.post(
        f"/sessions/{session.id}/score",
        json={
            "task_id": "T-DOCS",
            "points": 6,
            "comment": (
                "Кандидат верно описал базовую идею идемпотентности и корректно отметил, "
                "что POST обычно не считается идемпотентным. Не хватило более точного "
                "разведения HTTP-семантики и поведения конкретного API."
            ),
            "question_index": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_final"] is False
    assert payload["question_index"] == 1

    saved_score = (
        db_session.query(models.Score)
        .filter_by(session_id=session.id, task_id="T-DOCS", question_index=1)
        .one()
    )
    assert saved_score.is_final is False
    assert saved_score.score_type == "theory_intermediate"


def test_direct_final_theory_score_does_not_exceed_intermediate_avg(
    client,
    db_session,
    theory_scenario_factory,
):
    scenario = theory_scenario_factory()

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
                "Ответ неверный: идемпотентность перепутана с другой темой, а POST ошибочно назван всегда идемпотентным."
            ),
            task_id="T-DOCS",
        )
    )
    db_session.add(
        models.Score(
            session_id=session.id,
            task_id="T-DOCS",
            points=2.0,
            comment=(
                "Ответ содержит существенные ошибки и не даёт корректного определения. "
                "Базовое понимание темы выражено слабо."
            ),
            is_final=False,
            question_index=1,
            score_type="theory_intermediate",
        )
    )
    db_session.commit()

    response = client.post(
        f"/sessions/{session.id}/score",
        json={
            "task_id": "T-DOCS",
            "points": 8,
            "comment": (
                "Кандидат допустил несколько существенных ошибок и неверно раскрыл ключевые понятия. "
                "Итог по блоку остаётся слабым, несмотря на попытку дать ответ."
            ),
            "comments": [
                "Кандидат перепутал смысл идемпотентности и ошибочно назвал POST всегда идемпотентным, поэтому ответ остался слабым."
            ],
            "is_final": True,
            "question_index": None,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["points"] == 2.0

    db_session.refresh(session)
    assert session.scores["T-DOCS"] == 2.0
