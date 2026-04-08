from __future__ import annotations

import json

from app import models
from app.routers.sessions_api import practice as practice_module


def test_practice_code_replaces_placeholder_score_comment_and_completes_flow(
    client,
    db_session,
    monkeypatch,
):
    role = models.Role(name="Backend", slug="be-practice-code", description="coding role")
    db_session.add(role)
    db_session.flush()

    scenario = models.Scenario(
        role_id=role.id,
        name="Coding practice scenario",
        slug="coding-practice-scenario",
        description="scenario with coding task",
        difficulty="middle",
        tasks=[
            {
                "id": "C1",
                "type": "coding",
                "title": "Two sum",
                "language": "python",
                "description_for_candidate": "Реализуйте функцию two_sum.",
                "max_points": 10,
            }
        ],
        config={},
    )
    db_session.add(scenario)
    db_session.commit()
    db_session.refresh(scenario)

    session = models.Session(
        scenario_id=scenario.id,
        role_id=role.id,
        state="active",
        current_task_id=None,
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
            assert tool_names == {"run_code"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "run_code_call",
                                    "type": "function",
                                    "function": {
                                        "name": "run_code",
                                        "arguments": json.dumps(
                                            {
                                                "task_id": "C1",
                                                "language": "python",
                                                "code": "def two_sum(nums, target): return []",
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
                                                "task_id": "C1",
                                                "points": 2.0,
                                                "comment": (
                                                    "Корректность: [что работает, что не работает, опираясь на sandbox]\n"
                                                    "Качество кода: [читаемость, структура, нейминг, обработка крайних случаев]\n"
                                                    "Сложность и эффективность: [краткая оценка или фраза, что для этой задачи это несущественно]\n"
                                                    "Что можно улучшить: [1-3 конкретных улучшения]"
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
                            "Практическая проверка завершена.\n\n"
                            "Балл: 2/10\n\n"
                            "Решение пока проходит только часть проверок, но направление мысли читается. "
                            "Нужно исправить основную логику поиска пары и усилить обработку кейсов."
                        ),
                    }
                }
            ]
        }

    def fake_dispatch_tool_call(session_obj, tc, db):
        function = tc.get("function") or {}
        name = function.get("name")
        args = json.loads(function.get("arguments") or "{}")

        if name == "run_code":
            return {
                "ok": True,
                "task_id": "C1",
                "result": {
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 1,
                    "details": None,
                    "tests_total": 4,
                    "tests_passed": 1,
                    "test_results": [
                        {"name": "basic", "passed": True},
                        {"name": "duplicate values", "passed": False, "error": "expected [0, 1], got []"},
                        {"name": "negative values", "passed": False, "error": "expected [1, 2], got []"},
                        {"name": "no solution", "passed": False, "error": "expected [], got None"},
                    ],
                },
            }

        assert name == "score_task"
        comment = str(args.get("comment") or "")
        assert "[" not in comment
        assert "]" not in comment
        assert "заполни" not in comment.lower()
        assert "если применимо" not in comment.lower()
        assert "1-3 конкретных замечания" not in comment.lower()
        assert "Корректность:" in comment
        assert "Качество кода:" in comment
        assert "Сложность и эффективность:" in comment
        assert "Что можно улучшить:" in comment
        assert "duplicate values" in comment or "negative values" in comment or "no solution" in comment

        return {
            "ok": True,
            "task_id": "C1",
            "points": float(args.get("points") or 0),
            "comment": comment,
            "is_final": True,
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(practice_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    response = client.post(
        f"/sessions/{session.id}/practice/code",
        json={
            "task_id": "C1",
            "language": "python",
            "code": "def two_sum(nums, target): return []",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    tool_results = payload["tool_results"]

    assert tool_results[0]["name"] == "run_code"
    assert tool_results[0]["result"]["ok"] is True
    assert tool_results[1]["name"] == "score_task"
    assert tool_results[1]["result"]["ok"] is True
    assert "model did not complete required score_task step" not in payload["reply"]

    db_session.refresh(session)
    assert session.current_task_id == "C1"
