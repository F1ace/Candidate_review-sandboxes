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


def test_streaming_theory_flow_uses_contract_repair_and_final_points(
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

    state = {"summary_calls": 0, "tool_sequences": []}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            state["tool_sequences"].append(tool_names)
            assert "run_code" not in tool_names
            assert "run_sql" not in tool_names

            if "rag_search" in tool_names:
                args = {
                    "query": "идемпотентность POST",
                    "task_id": "T-DOCS",
                    "question_index": 1,
                    "top_k": 3,
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "rag_search_call",
                                        "type": "function",
                                        "function": {
                                            "name": "rag_search",
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            assert tool_names == {"score_task"}
            if "Все промежуточные оценки theory-блока уже сохранены" in system_text:
                args = {
                    "task_id": "T-DOCS",
                    "points": 4.0,
                    "comment": (
                        "Кандидат понимает базовый смысл идемпотентности и не путает типичную семантику POST. "
                        "При этом ответу не хватило точного разведения свойства HTTP-операции и поведения конкретного API."
                    ),
                    "comments": [
                        (
                            "Кандидат верно описал базовую идею идемпотентности и корректно сказал, что POST обычно "
                            "не считается идемпотентным, но не разделил свойство операции и конкретную реализацию API."
                        )
                    ],
                    "is_final": True,
                    "question_index": None,
                }
            else:
                args = {
                    "task_id": "T-DOCS",
                    "points": 7.0,
                    "comment": (
                        "Ответ в целом подтверждается документами сценария: кандидат правильно описал смысл идемпотентности "
                        "и отметил, что POST обычно не является идемпотентным. Не хватает более точного разделения между "
                        "свойством операции и частной реализацией API."
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

        state["summary_calls"] += 1
        assert "THEORY_FINAL_MESSAGE_CONTRACT" in system_text

        if state["summary_calls"] == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "Теоретический блок завершён. Итоговая оценка: 7/10. "
                                "Переходим к практической части."
                            ),
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
                            "**Итоги теоретической части**\n\n"
                            "Теоретический блок завершён. В ответе есть корректное базовое понимание темы, "
                            "но детализация оказалась недостаточной.\n\n"
                            "- **Идемпотентность и POST:** Кандидат верно описал базовую идею идемпотентности и корректно сказал, "
                            "что POST обычно не считается идемпотентным, но не разделил свойство операции и конкретную реализацию API.\n\n"
                            "**Сильные стороны:**\n"
                            "- Верно понимает базовый смысл идемпотентности.\n"
                            "- Не путает типичную семантику POST.\n\n"
                            "**Зоны роста:**\n"
                            "- Точнее разводить HTTP-семантику метода и поведение конкретного endpoint.\n"
                            "- Подробнее объяснять границы идемпотентности.\n\n"
                            "**Итоговая оценка по теоретическому блоку:** 4/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "**Итоговая оценка по теоретическому блоку:** 4/10." in final_text
    assert "Идемпотентность и POST" in final_text
    assert "Сильные стороны" in final_text
    assert "Зоны роста" in final_text
    assert "Переходим к практической части" not in final_text

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
    assert scores[1].points == 4.0
    assert len(state["tool_sequences"]) == 3
    assert "rag_search" in state["tool_sequences"][0]
    assert state["tool_sequences"][1] == {"score_task"}
    assert state["tool_sequences"][2] == {"score_task"}
    assert state["summary_calls"] == 2

    db_session.refresh(session)
    assert session.scores["T-DOCS"] == 4.0


def test_streaming_theory_rag_autofills_missing_final_comments_from_intermediate_scores(
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

    state = {"summary_calls": 0, "tool_sequences": []}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            state["tool_sequences"].append(tool_names)
            assert "run_code" not in tool_names
            assert "run_sql" not in tool_names

            if "rag_search" in tool_names:
                args = {
                    "query": "идемпотентность POST",
                    "task_id": "T-DOCS",
                    "question_index": 1,
                    "top_k": 3,
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "rag_search_call",
                                        "type": "function",
                                        "function": {
                                            "name": "rag_search",
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            assert tool_names == {"score_task"}
            if "Все промежуточные оценки theory-блока уже сохранены" in system_text:
                args = {
                    "task_id": "T-DOCS",
                    "points": 6.0,
                    "comment": (
                        "Кандидат уверенно понимает базовую идею идемпотентности и корректно связывает её с POST. "
                        "Для более сильного результата не хватило более точного объяснения границ между HTTP-семантикой и поведением конкретного API."
                    ),
                    "is_final": True,
                    "question_index": None,
                }
            else:
                args = {
                    "task_id": "T-DOCS",
                    "points": 6.0,
                    "comment": (
                        "Ответ в целом подтверждается документами сценария: кандидат правильно описал смысл идемпотентности "
                        "и отметил, что POST обычно не является идемпотентным. Не хватает более точного разделения между "
                        "свойством операции и частной реализацией API."
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

        state["summary_calls"] += 1
        assert "THEORY_FINAL_MESSAGE_CONTRACT" in system_text
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "**Итоги теоретической части**\n\n"
                            "Теоретический блок завершён. Кандидат показал хорошее базовое понимание темы, "
                            "но часть деталей объяснил слишком общо.\n\n"
                            "- **Идемпотентность и POST:** Кандидат корректно объяснил базовую идею идемпотентности "
                            "и верно отметил, что POST обычно не считается идемпотентным, но не раскрыл, "
                            "где заканчивается свойство метода и начинается поведение конкретного API.\n\n"
                            "**Сильные стороны:**\n"
                            "- Понимает базовую HTTP-семантику.\n\n"
                            "**Зоны роста:**\n"
                            "- Добавлять больше технической конкретики в развёрнутый ответ.\n\n"
                            "**Итоговая оценка по теоретическому блоку:** 6/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "**Итоговая оценка по теоретическому блоку:** 6/10." in final_text
    assert "Идемпотентность и POST" in final_text

    tool_messages = (
        db_session.query(models.Message)
        .filter_by(session_id=session.id, sender="tool")
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )
    assert len(tool_messages) == 3
    assert all("Final theory comments are required" not in (message.text or "") for message in tool_messages)
    assert "'comments': [" in (tool_messages[-1].text or "")

    scores = (
        db_session.query(models.Score)
        .filter_by(session_id=session.id, task_id="T-DOCS")
        .order_by(models.Score.created_at.asc(), models.Score.id.asc())
        .all()
    )
    assert len(scores) == 2
    assert scores[0].is_final is False
    assert scores[1].is_final is True
    assert scores[1].points == 6.0
    assert state["tool_sequences"] == [{"rag_search"}, {"score_task"}, {"score_task"}]
    assert state["summary_calls"] == 1
    assert embeddings_backend.document_calls
    assert embeddings_backend.query_calls


def test_streaming_theory_flow_coerces_missing_is_final_for_intermediate_score(
    client,
    db_session,
    theory_scenario_factory,
    monkeypatch,
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

    state = {"score_calls": 0, "summary_calls": 0, "tool_sequences": []}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            state["tool_sequences"].append(tool_names)
            assert tool_names == {"score_task"}

            if state["score_calls"] == 0:
                args = {
                    "task_id": "T-DOCS",
                    "points": 7.0,
                    "comment": (
                        "Кандидат правильно описал базовый смысл идемпотентности и верно отметил, "
                        "что POST обычно не считается идемпотентным. Не хватило более точного "
                        "объяснения границ между свойством метода и поведением конкретного API."
                    ),
                    "question_index": 1,
                }
            else:
                assert "Все промежуточные оценки theory-блока уже сохранены" in system_text
                args = {
                    "task_id": "T-DOCS",
                    "points": 6.0,
                    "comment": (
                        "Кандидат уверенно понимает базовую идею темы и не путает семантику POST. "
                        "Для более сильного результата не хватило точности в деталях и более строгого "
                        "разведения уровней абстракции."
                    ),
                    "comments": [
                        (
                            "Кандидат верно описал базовую идею идемпотентности и корректно связал её с POST, "
                            "но не раскрыл, где заканчивается свойство HTTP-метода и начинается поведение конкретного API."
                        )
                    ],
                    "is_final": True,
                    "question_index": None,
                }

            state["score_calls"] += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": f"score_task_call_{state['score_calls']}",
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

        state["summary_calls"] += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "**Итоги теоретической части**\n\n"
                            "Теоретический блок завершён. Кандидат показал хорошее базовое понимание темы, "
                            "но часть важных границ объяснил слишком общо.\n\n"
                            "- **Идемпотентность и POST:** Кандидат верно описал базовую идею идемпотентности и "
                            "корректно связал её с POST, но не раскрыл, где заканчивается свойство HTTP-метода "
                            "и начинается поведение конкретного API.\n\n"
                            "**Сильные стороны:**\n"
                            "- Хорошо понимает базовую идею идемпотентности.\n"
                            "- Не путает типичную семантику POST.\n\n"
                            "**Зоны роста:**\n"
                            "- Точнее разводить HTTP-семантику и реализацию endpoint.\n"
                            "- Давать больше технической конкретики в объяснениях.\n\n"
                            "**Итоговая оценка по теоретическому блоку:** 6/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "**Итоговая оценка по теоретическому блоку:** 6/10." in final_text
    assert "Идемпотентность и POST" in final_text
    assert "Не удалось автоматически сформировать итоговую оценку" not in final_text

    scores = (
        db_session.query(models.Score)
        .filter_by(session_id=session.id, task_id="T-DOCS")
        .order_by(models.Score.created_at.asc(), models.Score.id.asc())
        .all()
    )
    assert len(scores) == 2
    assert scores[0].is_final is False
    assert scores[0].question_index == 1
    assert scores[1].is_final is True
    assert scores[1].points == 6.0

    tool_messages = (
        db_session.query(models.Message)
        .filter_by(session_id=session.id, sender="tool")
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )
    assert len(tool_messages) == 2
    assert all("Final theory comments are required" not in (message.text or "") for message in tool_messages)
    assert state["tool_sequences"] == [{"score_task"}, {"score_task"}]
    assert state["summary_calls"] == 1


def test_streaming_theory_flow_repairs_comments_only_final_summary(
    client,
    db_session,
    theory_scenario_factory,
    monkeypatch,
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

    state = {"score_calls": 0, "summary_calls": 0, "tool_sequences": []}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            state["tool_sequences"].append(tool_names)
            assert tool_names == {"score_task"}

            if state["score_calls"] == 0:
                args = {
                    "task_id": "T-DOCS",
                    "points": 8.0,
                    "comment": (
                        "Кандидат правильно описал базовый смысл идемпотентности и верно отметил, "
                        "что POST обычно не считается идемпотентным."
                    ),
                    "question_index": 1,
                }
            else:
                assert "Все промежуточные оценки theory-блока уже сохранены" in system_text
                args = {
                    "task_id": "T-DOCS",
                    "points": 8.0,
                    "comment": (
                        "Кандидат показал хорошее понимание ключевых понятий и уверенно раскрыл базовую тему. "
                        "Для более сильного результата полезно добавить больше прикладных деталей и примеров."
                    ),
                    "comments": [
                        (
                            "Кандидат правильно объяснил базовую идею идемпотентности и корректно отметил, "
                            "что POST обычно не считается идемпотентным."
                        )
                    ],
                    "is_final": True,
                    "question_index": None,
                }

            state["score_calls"] += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": f"score_task_call_{state['score_calls']}",
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

        state["summary_calls"] += 1
        if state["summary_calls"] == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "**Комментарии по каждому ответу**\n\n"
                                "- **1. Что такое идемпотентность и как она связана с POST?:** "
                                "Кандидат правильно объяснил базовую идею идемпотентности и корректно отметил, "
                                "что POST обычно не считается идемпотентным."
                            ),
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
                            "Теоретический блок завершён.\n\n"
                            "**Комментарии по каждому ответу**\n\n"
                            "- **1. Что такое идемпотентность и как она связана с POST?:** "
                            "Кандидат правильно объяснил базовую идею идемпотентности и корректно отметил, "
                            "что POST обычно не считается идемпотентным.\n\n"
                            "**Сильные стороны**\n\n"
                            "- Кандидат уверенно ориентируется в базовой HTTP-семантике.\n\n"
                            "**Зоны роста**\n\n"
                            "- Полезно добавить больше прикладных деталей и примеров поведения API.\n\n"
                            "**Итоговая оценка по теоретическому блоку:** 8/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "Теоретический блок завершён." in final_text
    assert "Комментарии по каждому ответу" in final_text
    assert "Сильные стороны" in final_text
    assert "Зоны роста" in final_text
    assert "**Итоговая оценка по теоретическому блоку:** 8/10." in final_text
    assert "Кандидат правильно объяснил базовую идею идемпотентности" in final_text
    assert state["tool_sequences"] == [{"score_task"}, {"score_task"}]
    assert state["summary_calls"] == 2


def test_streaming_theory_rag_flow_clamps_intermediate_points_below_one(
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
                "L1 и L2 регуляризация нужны только для деревьев решений, а идемпотентность означает "
                "ускорение обучения сети. POST всегда идемпотентен."
            ),
            task_id="T-DOCS",
        )
    )
    db_session.commit()

    state = {"summary_calls": 0, "tool_sequences": []}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            state["tool_sequences"].append(tool_names)

            if "rag_search" in tool_names:
                args = {
                    "query": "идемпотентность POST",
                    "task_id": "T-DOCS",
                    "question_index": 1,
                    "top_k": 3,
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "rag_search_call",
                                        "type": "function",
                                        "function": {
                                            "name": "rag_search",
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            assert tool_names == {"score_task"}
            if "Все промежуточные оценки theory-блока уже сохранены" in system_text:
                args = {
                    "task_id": "T-DOCS",
                    "points": 3.0,
                    "comment": (
                        "Кандидат допустил несколько существенных ошибок и спутал ключевые понятия. "
                        "При этом ответ всё же показывает попытку рассуждать по теме, поэтому минимальная база для обсуждения есть."
                    ),
                    "comments": [
                        (
                            "Кандидат перепутал идемпотентность с другими темами, ошибочно назвал POST всегда идемпотентным "
                            "и не дал корректного определения свойства повторного вызова."
                        )
                    ],
                    "is_final": True,
                    "question_index": None,
                }
            else:
                args = {
                    "task_id": "T-DOCS",
                    "points": 0.0,
                    "comment": (
                        "Ответ противоречит материалам сценария: кандидат перепутал тему идемпотентности с регуляризацией "
                        "и неверно утверждает, что POST всегда является идемпотентным. По существу вопроса ответ получился некорректным."
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

        state["summary_calls"] += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "**Итоги теоретической части**\n\n"
                            "Теоретический блок завершён. В ответе заметны существенные пробелы в понимании темы, "
                            "но попытка рассуждать всё же была.\n\n"
                            "- **Идемпотентность и POST:** Кандидат перепутал идемпотентность с другими темами, ошибочно назвал "
                            "POST всегда идемпотентным и не дал корректного определения свойства повторного вызова.\n\n"
                            "**Сильные стороны:**\n"
                            "- Пытается формулировать ответ, а не полностью уходит от вопроса.\n\n"
                            "**Зоны роста:**\n"
                            "- Разобраться в базовом определении идемпотентности.\n"
                            "- Не смешивать HTTP-семантику с нерелевантными ML-темами.\n\n"
                            "**Итоговая оценка по теоретическому блоку:** 1/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "**Итоговая оценка по теоретическому блоку:** 1/10." in final_text
    assert "Не удалось автоматически сформировать итоговую оценку" not in final_text

    scores = (
        db_session.query(models.Score)
        .filter_by(session_id=session.id, task_id="T-DOCS")
        .order_by(models.Score.created_at.asc(), models.Score.id.asc())
        .all()
    )
    assert len(scores) == 2
    assert scores[0].is_final is False
    assert scores[0].points == 1.0
    assert scores[1].is_final is True
    assert scores[1].points == 1.0

    tool_messages = (
        db_session.query(models.Message)
        .filter_by(session_id=session.id, sender="tool")
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )
    assert len(tool_messages) == 3
    assert all("Theory score should be within [1, 10]" not in (message.text or "") for message in tool_messages)
    assert state["tool_sequences"] == [{"rag_search"}, {"score_task"}, {"score_task"}]
    assert state["summary_calls"] == 1


def test_streaming_theory_rag_final_message_cleans_comment_table_garbage(
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

    state = {"summary_calls": 0}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            if "rag_search" in tool_names:
                args = {
                    "query": "идемпотентность POST",
                    "task_id": "T-DOCS",
                    "question_index": 1,
                    "top_k": 3,
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "rag_search_call",
                                        "type": "function",
                                        "function": {
                                            "name": "rag_search",
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            assert tool_names == {"score_task"}
            if "Все промежуточные оценки theory-блока уже сохранены" in system_text:
                args = {
                    "task_id": "T-DOCS",
                    "points": 4.0,
                    "comment": (
                        "Кандидат понимает базовый смысл идемпотентности и верно не считает POST автоматически идемпотентным. "
                        "При этом ответу не хватило более точного разведения свойства операции и реализации конкретного API."
                    ),
                    "comments": [
                        (
                            "Кандидат верно объяснил базовую идею идемпотентности и корректно отметил, что POST обычно "
                            "не считается идемпотентным, но не раскрыл границу между свойством метода и конкретной реализацией API."
                        )
                    ],
                    "is_final": True,
                    "question_index": None,
                }
            else:
                args = {
                    "task_id": "T-DOCS",
                    "points": 4.0,
                    "comment": (
                        "Ответ подтверждается документами сценария и правильно описывает базовую идею идемпотентности. "
                        "Не хватает более точного объяснения связи с конкретным поведением POST."
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

        state["summary_calls"] += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Блок завершён\n\n"
                            "Комментарии по каждому ответу\n\n"
                            "| Вопрос | Что было правильно | Что нужно улучшить |\n"
                            "|--------|--------------------|--------------------|\n"
                            "| 1 | База есть | Нужна детализация |\n\n"
                            "**Сильные стороны:**\n"
                            "- Есть базовое понимание темы.\n\n"
                            "**Зоны роста:**\n"
                            "- Нужна лучшая детализация HTTP-семантики.\n\n"
                            "Комментарии по каждому ответу\n\n"
                            "- **1. Черновик вопроса:** старый второй вариант, который тоже нужно убрать.\n\n"
                            "**Итоговая оценка по теоретическому блоку:** 4/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "Комментарии по каждому ответу" in final_text
    assert final_text.count("Комментарии по каждому ответу") == 1
    assert "Кандидат верно объяснил базовую идею идемпотентности" in final_text
    assert "| Вопрос |" not in final_text
    assert "|--------|" not in final_text
    assert final_text.index("Комментарии по каждому ответу") < final_text.index("Сильные стороны")
    assert state["summary_calls"] >= 1


def test_streaming_theory_rag_final_message_removes_duplicate_alternative_comment_header(
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

    state = {"summary_calls": 0}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            if "rag_search" in tool_names:
                args = {
                    "query": "идемпотентность POST",
                    "task_id": "T-DOCS",
                    "question_index": 1,
                    "top_k": 3,
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "rag_search_call",
                                        "type": "function",
                                        "function": {
                                            "name": "rag_search",
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            assert tool_names == {"score_task"}
            if "Все промежуточные оценки theory-блока уже сохранены" in system_text:
                args = {
                    "task_id": "T-DOCS",
                    "points": 4.0,
                    "comment": (
                        "Кандидат понимает базовый смысл идемпотентности и верно не считает POST автоматически идемпотентным. "
                        "При этом ответу не хватило более точного разведения свойства операции и реализации конкретного API."
                    ),
                    "comments": [
                        (
                            "Кандидат верно объяснил базовую идею идемпотентности и корректно отметил, что POST обычно "
                            "не считается идемпотентным, но не раскрыл границу между свойством метода и конкретной реализацией API."
                        )
                    ],
                    "is_final": True,
                    "question_index": None,
                }
            else:
                args = {
                    "task_id": "T-DOCS",
                    "points": 4.0,
                    "comment": (
                        "Ответ подтверждается документами сценария и правильно описывает базовую идею идемпотентности. "
                        "Не хватает более точного объяснения связи с конкретным поведением POST."
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

        state["summary_calls"] += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Вопросы теоретического блока завершены.\n\n"
                            "Комментарий к каждому ответу кандидата\n\n"
                            "- Вопрос 1/1: старый мусорный вариант.\n\n"
                            "Сильные стороны кандидата\n\n"
                            "- Есть базовое понимание темы.\n\n"
                            "Комментарии по каждому ответу\n\n"
                            "- **1. Черновик вопроса:** второй дубль, который тоже нужно убрать.\n\n"
                            "Зоны роста\n\n"
                            "- Нужна лучшая детализация HTTP-семантики.\n\n"
                            "Оценка\n\n"
                            "Кандидат набрал 4 из 10 возможных баллов."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "Комментарий к каждому ответу кандидата" not in final_text
    assert final_text.count("Комментарии по каждому ответу") == 1
    assert "старый мусорный вариант" not in final_text
    assert "Кандидат верно объяснил базовую идею идемпотентности" in final_text
    assert final_text.index("Комментарии по каждому ответу") < final_text.index("Сильные стороны кандидата")
    assert final_text.index("Сильные стороны кандидата") < final_text.index("Зоны роста")
    assert state["summary_calls"] >= 1


def test_streaming_theory_rag_final_message_removes_prefix_table_garbage_before_comments(
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

    state = {"summary_calls": 0}

    def fake_chat(messages, tools=None, tool_choice=None, temperature=0.2):
        system_text = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        tool_names = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if (tool.get("function") or {}).get("name")
        }

        if tools:
            if "rag_search" in tool_names:
                args = {
                    "query": "идемпотентность POST",
                    "task_id": "T-DOCS",
                    "question_index": 1,
                    "top_k": 3,
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "rag_search_call",
                                        "type": "function",
                                        "function": {
                                            "name": "rag_search",
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

            assert tool_names == {"score_task"}
            if "Все промежуточные оценки theory-блока уже сохранены" in system_text:
                args = {
                    "task_id": "T-DOCS",
                    "points": 8.0,
                    "comment": (
                        "Кандидат правильно раскрыл тему и показал хорошее понимание ключевых концепций. "
                        "До максимального результата не хватило лишь пары практических уточнений."
                    ),
                    "comments": [
                        (
                            "Кандидат правильно различил задачи регрессии и классификации, привёл понятные примеры "
                            "и не перепутал типы целевых переменных."
                        )
                    ],
                    "is_final": True,
                    "question_index": None,
                }
            else:
                args = {
                    "task_id": "T-DOCS",
                    "points": 8.0,
                    "comment": (
                        "Ответ подтверждается документами сценария и демонстрирует хорошее понимание базовой идеи "
                        "идемпотентности и поведения POST."
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

        state["summary_calls"] += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Блок завершён\n\n"
                            "Кандидат продемонстрировал хорошее понимание фундаментальных концепций машинного обучения:\n\n"
                            "| Вопрос | Оценка ||--------|---------|| 1. Регрессия vs классификация | ✅ |\n\n"
                            "Комментарии по каждому ответу\n\n"
                            "- **1. Черновик вопроса:** старый дубль, который нужно убрать.\n\n"
                            "Сильные стороны:\n\n"
                            "- Чёткое различие между задачами и примерами.\n\n"
                            "Зоны роста:\n\n"
                            "- Добавить больше практических уточнений.\n\n"
                            "Итоговая оценка: 8/10."
                        ),
                    }
                }
            ]
        }

    monkeypatch.setattr(streaming_module.lm_client, "chat", fake_chat)

    response = client.get(f"/sessions/{session.id}/lm/chat-stream")
    assert response.status_code == 200
    final_text = _parse_sse_done_content(response.text)

    assert "Кандидат продемонстрировал хорошее понимание фундаментальных концепций машинного обучения:" not in final_text
    assert "| Вопрос |" not in final_text
    assert "✅" not in final_text
    assert final_text.count("Комментарии по каждому ответу") == 1
    assert "Кандидат правильно различил задачи регрессии и классификации" in final_text
    assert final_text.index("Комментарии по каждому ответу") < final_text.index("Сильные стороны")
    assert state["summary_calls"] >= 1
