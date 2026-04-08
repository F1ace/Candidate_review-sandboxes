from __future__ import annotations

import json

from app import models
from app.routers.sessions_api import practice as practice_module
from app.routers.sessions_api import dispatch as dispatch_module


def test_practice_sql_sets_current_task_and_allows_run_sql(client, db_session, monkeypatch):
    role = models.Role(name="Data Analyst", slug="da-practice-sql", description="sql role")
    db_session.add(role)
    db_session.flush()

    scenario = models.Scenario(
        role_id=role.id,
        name="SQL practice scenario",
        slug="sql-practice-scenario",
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

    sql_task = models.Task(
        scenario_id=scenario.id,
        external_id="SQL1",
        task_type="sql",
        title="Orders by city",
        description_for_candidate="Посчитайте оплаченные отгрузки по городам.",
        max_points=10,
        order_index=1,
        sql_scenario_ref="orders_city",
    )
    db_session.add(sql_task)

    session = models.Session(
        scenario_id=scenario.id,
        role_id=role.id,
        state="active",
        current_task_id="T1",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

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
                            "Оценка: 10/10\n\n"
                            "Корректность: Запрос корректно агрегирует оплаченные и shipped-заказы по городам.\n"
                            "Качество решения: Решение короткое и читаемое, без лишних вложенностей.\n"
                            "Работа с SQL: Корректно использованы фильтрация и группировка.\n"
                            "Что можно улучшить: Можно явно зафиксировать порядок сортировки результата, если он важен."
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
    assert "Оценка: 10/10" in payload["reply"]

    db_session.refresh(session)
    assert session.current_task_id == "SQL1"
