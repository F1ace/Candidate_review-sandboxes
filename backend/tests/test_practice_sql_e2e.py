from __future__ import annotations

import json

from app import models
from app.routers.sessions_api import practice as practice_module
from app.routers.sessions_api import dispatch as dispatch_module


def _create_sql_session(db_session, *, slug: str) -> models.Session:
    role = models.Role(name=f"Role {slug}", slug=slug, description="sql role")
    db_session.add(role)
    db_session.flush()

    scenario = models.Scenario(
        role_id=role.id,
        name=f"SQL scenario {slug}",
        slug=f"sql-scenario-{slug}",
        description="scenario with theory and sql",
        difficulty="middle",
        tasks=[
            {
                "id": "T1",
                "type": "theory",
                "title": "Theory block",
                "max_points": 10,
                "questions": ["Что такое оконная функция?"],
            },
            {
                "id": "SQL1",
                "type": "sql",
                "title": "Orders by city",
                "description_for_candidate": "Посчитайте оплаченные отгрузки по городам.",
                "max_points": 10,
                "sql_scenario_id": "orders_city",
            },
        ],
        config={},
    )
    db_session.add(scenario)
    db_session.flush()

    db_session.add(
        models.Task(
            scenario_id=scenario.id,
            external_id="SQL1",
            task_type="sql",
            title="Orders by city",
            description_for_candidate="Посчитайте оплаченные отгрузки по городам.",
            max_points=10,
            order_index=1,
            sql_scenario_ref="orders_city",
        )
    )

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


def test_practice_sql_sets_current_task_and_allows_run_sql(client, db_session, monkeypatch):
    session = _create_sql_session(db_session, slug="da-practice-sql")

    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1

        if step == 0:
            tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}
            assert tool_names == {"run_sql"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "run_sql_call",
                                    "type": "function",
                                    "function": {
                                        "name": "run_sql",
                                        "arguments": json.dumps(
                                            {
                                                "task_id": "SQL1",
                                                "query": (
                                                    "select city, count(*) as shipped_paid_orders "
                                                    "from orders where status in ('paid', 'shipped') "
                                                    "group by city"
                                                ),
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

        if step == 1:
            tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}
            assert tool_names == {"score_task"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "score_task_call",
                                    "type": "function",
                                    "function": {
                                        "name": "score_task",
                                        "arguments": json.dumps(
                                            {
                                                "task_id": "SQL1",
                                                "points": 10.0,
                                                "comment": (
                                                    "Корректность: Запрос корректно агрегирует оплаченные и shipped-заказы по городам.\n"
                                                    "Качество решения: Решение короткое и читаемое, без лишних вложенностей.\n"
                                                    "Работа с SQL: Корректно использованы фильтрация и группировка.\n"
                                                    "Что можно улучшить: Можно явно зафиксировать порядок сортировки результата, если он важен."
                                                ),
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

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "SQL-проверка завершена.\n\n"
                            "Корректность: запрос корректно агрегирует оплаченные и shipped-заказы по городам, "
                            "поэтому основная логика решения выглядит верной.\n\n"
                            "Качество решения: запрос получился компактным и читаемым, без лишних вложенностей.\n\n"
                            "Работа с SQL: фильтрация и группировка использованы уместно и по делу.\n\n"
                            "Что можно улучшить: если порядок строк важен для задачи, его стоит явно зафиксировать через order by."
                        ),
                    }
                }
            ]
        }

    def fake_run_sql_for_task(*, db, task_row, query):
        assert task_row.external_id == "SQL1"
        return {
            "ok": True,
            "task_id": "SQL1",
            "sql_scenario_id": "orders_city",
            "result": {
                "success": True,
                "columns": ["city", "shipped_paid_orders"],
                "rows": [["Moscow", 3]],
                "error": None,
            },
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(dispatch_module.sql_runner, "run_sql_for_task", fake_run_sql_for_task)

    response = client.post(
        f"/sessions/{session.id}/practice/sql",
        json={
            "task_id": "SQL1",
            "sql_scenario_id": "orders_city",
            "query": (
                "select city, count(*) as shipped_paid_orders "
                "from orders where status in ('paid', 'shipped') group by city"
            ),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    tool_results = payload["tool_results"]

    assert tool_results[0]["tool"] == "run_sql"
    assert tool_results[0]["result"]["ok"] is True
    assert "run_sql is not available in the current block" not in str(tool_results)

    assert tool_results[1]["tool"] == "score_task"
    assert tool_results[1]["result"]["ok"] is True
    assert payload["reply_source"] == "model"
    assert "SQL-проверка завершена." in payload["reply"]
    assert "Корректность:" in payload["reply"]
    assert "Работа с SQL:" in payload["reply"]
    assert "Оценка: 10/10" not in payload["reply"]

    db_session.refresh(session)
    assert session.current_task_id == "SQL1"


def test_practice_sql_converts_plain_feedback_into_score_task_and_preserves_model_reply(
    client,
    db_session,
    monkeypatch,
):
    session = _create_sql_session(db_session, slug="sql-plain-feedback")
    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_sql"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "run_sql_call",
                                    "type": "function",
                                    "function": {
                                        "name": "run_sql",
                                        "arguments": json.dumps(
                                            {
                                                "task_id": "SQL1",
                                                "query": (
                                                    "select city, count(*) as shipped_paid_orders "
                                                    "from orders where status in ('paid', 'shipped') "
                                                    "group by city"
                                                ),
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

        if step == 1:
            assert tool_names == {"score_task"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "Оценка: 9/10\n\n"
                                "Корректность: Запрос возвращает ожидаемую агрегацию по городам и корректно учитывает нужные статусы.\n"
                                "Качество решения: Решение получилось компактным и читаемым, без лишних подзапросов.\n"
                                "Работа с SQL: Фильтрация и группировка использованы по делу, логика выборки выглядит последовательной.\n"
                                "Что можно улучшить: Если порядок строк важен, стоит явно добавить order by и зафиксировать сортировку результата."
                            ),
                        }
                    }
                ]
            }

        assert tool_names == set()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "SQL-проверка завершена.\n\n"
                            "Корректность: запрос дал ожидаемый результат и корректно собрал агрегацию по городам.\n\n"
                            "Качество решения: запрос короткий и читаемый, без лишней сложности.\n\n"
                            "Работа с SQL: фильтрация и группировка использованы уместно и соответствуют задаче.\n\n"
                            "Что можно улучшить: можно явно зафиксировать сортировку, если она важна для итоговой выдачи."
                        ),
                    }
                }
            ]
        }

    def fake_run_sql_for_task(*, db, task_row, query):
        assert task_row.external_id == "SQL1"
        return {
            "ok": True,
            "task_id": "SQL1",
            "sql_scenario_id": "orders_city",
            "result": {
                "success": True,
                "columns": ["city", "shipped_paid_orders"],
                "rows": [["Moscow", 3]],
                "error": None,
            },
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(dispatch_module.sql_runner, "run_sql_for_task", fake_run_sql_for_task)

    response = client.post(
        f"/sessions/{session.id}/practice/sql",
        json={
            "task_id": "SQL1",
            "sql_scenario_id": "orders_city",
            "query": (
                "select city, count(*) as shipped_paid_orders "
                "from orders where status in ('paid', 'shipped') group by city"
            ),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    tool_results = payload["tool_results"]

    assert tool_results[0]["tool"] == "run_sql"
    assert tool_results[0]["result"]["ok"] is True
    assert tool_results[-1]["tool"] == "score_task"
    assert tool_results[-1]["result"]["ok"] is True
    assert tool_results[-1]["result"]["points"] == 9
    assert "Корректность:" in tool_results[-1]["result"]["comment"]
    assert "Качество решения:" in tool_results[-1]["result"]["comment"]
    assert "Работа с SQL:" in tool_results[-1]["result"]["comment"]
    assert "Что можно улучшить:" in tool_results[-1]["result"]["comment"]
    assert payload["reply_source"] == "model"
    assert "SQL-проверка завершена." in payload["reply"]
    assert "Оценка: 9/10" not in payload["reply"]


def test_practice_sql_strips_points_and_comment_wrappers_from_final_reply(
    client,
    db_session,
    monkeypatch,
):
    session = _create_sql_session(db_session, slug="sql-reply-normalization")
    state = {"step": 0}

    sql_comment = (
        "Корректность: Результат содержит неверный коэффициент конверсии из-за целочисленного деления и подсчёта всех покупок, а не уникальных пользователей.\n"
        "Качество решения: Запрос читаемый, но отсутствует alias для таблиц и форматирование можно сделать аккуратнее.\n"
        "Работа с SQL: LEFT JOIN и GROUP BY использованы уместно, но COUNT без DISTINCT приводит к двойному счёту и не хватает приведения типов.\n"
        "Что можно улучшить: Использовать COUNT(DISTINCT e.user_id), привести результат к дроби через CAST и при необходимости добавить фильтр по дате регистрации."
    )

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_sql"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "run_sql_call",
                                    "type": "function",
                                    "function": {
                                        "name": "run_sql",
                                        "arguments": json.dumps(
                                            {
                                                "task_id": "SQL1",
                                                "query": (
                                                    "select city, count(*) as shipped_paid_orders "
                                                    "from orders where status in ('paid', 'shipped') "
                                                    "group by city"
                                                ),
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

        if step == 1:
            assert tool_names == {"score_task"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "score_task_call",
                                    "type": "function",
                                    "function": {
                                        "name": "score_task",
                                        "arguments": json.dumps(
                                            {
                                                "task_id": "SQL1",
                                                "points": 5.0,
                                                "comment": sql_comment,
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

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"Points: 5\n\nComment:\n{sql_comment}",
                    }
                }
            ]
        }

    def fake_run_sql_for_task(*, db, task_row, query):
        assert task_row.external_id == "SQL1"
        return {
            "ok": True,
            "task_id": "SQL1",
            "sql_scenario_id": "orders_city",
            "result": {
                "success": True,
                "columns": ["city", "shipped_paid_orders"],
                "rows": [["Moscow", 3]],
                "error": None,
            },
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(dispatch_module.sql_runner, "run_sql_for_task", fake_run_sql_for_task)

    response = client.post(
        f"/sessions/{session.id}/practice/sql",
        json={
            "task_id": "SQL1",
            "sql_scenario_id": "orders_city",
            "query": (
                "select city, count(*) as shipped_paid_orders "
                "from orders where status in ('paid', 'shipped') group by city"
            ),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply_source"] == "model"
    assert "Points: 5" not in payload["reply"]
    assert "Comment:" not in payload["reply"]
    assert "Корректность:" in payload["reply"]
    assert "Работа с SQL:" in payload["reply"]
