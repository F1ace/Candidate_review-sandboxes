from __future__ import annotations

import json

from app import models
from app.routers.sessions_api import practice as practice_module


VALID_SCORE_COMMENT = (
    "Корректность: Решение проходит sandbox-проверки и на текущем наборе кейсов ведет себя корректно.\n"
    "Качество кода: Код читается легко и не выглядит перегруженным, хотя пару мест можно сделать чуть более самодокументируемыми.\n"
    "Сложность и эффективность: Для этой задачи выбранная реализация выглядит достаточно эффективной и не вызывает заметных рисков.\n"
    "Что можно улучшить: Добавить пару собственных тестов на крайние случаи и при желании чуть подробнее назвать промежуточные сущности."
)


def _create_code_session(db_session, *, slug: str, max_points: int = 10) -> models.Session:
    role = models.Role(name=f"Role {slug}", slug=slug, description="coding role")
    db_session.add(role)
    db_session.flush()

    scenario = models.Scenario(
        role_id=role.id,
        name=f"Scenario {slug}",
        slug=f"scenario-{slug}",
        description="scenario with coding task",
        difficulty="middle",
        tasks=[
            {
                "id": "C1",
                "type": "coding",
                "title": "Two sum",
                "language": "python",
                "description_for_candidate": "Implement two_sum.",
                "max_points": max_points,
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
    return session


def _run_code_result(*, success: bool, tests_passed: int, tests_total: int = 4) -> dict:
    test_results = [
        {"name": "basic", "passed": True, "error": None},
        {"name": "duplicate values", "passed": success or tests_passed >= 2, "error": None if success or tests_passed >= 2 else "expected [0, 1], got []"},
        {"name": "negative values", "passed": success or tests_passed >= 3, "error": None if success or tests_passed >= 3 else "expected [1, 2], got []"},
        {"name": "no solution", "passed": success or tests_passed >= 4, "error": None if success or tests_passed >= 4 else "expected [], got None"},
    ]
    cleaned_results = []
    for item in test_results:
        payload = {"name": item["name"], "passed": item["passed"]}
        if item["error"] is not None:
            payload["error"] = item["error"]
        cleaned_results.append(payload)

    return {
        "ok": True,
        "task_id": "C1",
        "result": {
            "success": success,
            "stdout": "",
            "stderr": "",
            "exit_code": 0 if success else 1,
            "details": None,
            "tests_total": tests_total,
            "tests_passed": tests_passed,
            "test_results": cleaned_results,
        },
    }


def _score_tool_call(*, points: float, comment: str, tool_id: str = "score_task_call") -> dict:
    return {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": "score_task",
            "arguments": json.dumps(
                {
                    "task_id": "C1",
                    "points": points,
                    "comment": comment,
                },
                ensure_ascii=False,
            ),
        },
    }


def _run_code_tool_call(code: str = "def two_sum(nums, target): return [0, 1]") -> dict:
    return {
        "id": "run_code_call",
        "type": "function",
        "function": {
            "name": "run_code",
            "arguments": json.dumps(
                {
                    "task_id": "C1",
                    "language": "python",
                    "code": code,
                },
                ensure_ascii=False,
            ),
        },
    }


def test_practice_code_retries_invalid_score_comment_and_finishes(
    client,
    db_session,
    monkeypatch,
):
    session = _create_code_session(db_session, slug="be-practice-code")
    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_code"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_run_code_tool_call("def two_sum(nums, target): return []")]}}]}

        if step == 1:
            assert tool_names == {"score_task"}
            invalid_comment = (
                "Корректность: [что работает]\n"
                "Качество кода: [что не так]\n"
                "Сложность и эффективность: [оценка]\n"
                "Что можно улучшить: [1-3 улучшения]"
            )
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_score_tool_call(points=2.0, comment=invalid_comment)]}}]}

        if step == 2:
            assert tool_names == {"score_task"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_score_tool_call(points=2.0, comment=VALID_SCORE_COMMENT, tool_id="score_task_retry_call")]}}]}

        assert tool_names == set()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Практическая проверка завершена.\n\n"
                            "Корректность: Решение пока проходит только часть проверок и требует доработки основной логики.\n"
                            "Качество кода: Сейчас коду не хватает явной структуры для проблемных сценариев.\n"
                            "Сложность и эффективность: Пока важнее восстановить корректность, чем отдельно обсуждать асимптотику.\n"
                            "Что можно улучшить: Исправить поиск пары и добавить локальные тесты на дубликаты и отрицательные значения."
                        ),
                    }
                }
            ]
        }

    def fake_dispatch_tool_call(session_obj, tc, db):
        name = (tc.get("function") or {}).get("name")
        args = json.loads(((tc.get("function") or {}).get("arguments") or "{}"))

        if name == "run_code":
            return _run_code_result(success=False, tests_passed=1)

        assert name == "score_task"
        comment = str(args.get("comment") or "")
        if "[" in comment or "]" in comment:
            return {
                "ok": False,
                "task_id": "C1",
                "error": "Practice comment contains placeholders or template instructions instead of final feedback.",
            }
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
        json={"task_id": "C1", "language": "python", "code": "def two_sum(nums, target): return []"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_results"][0]["name"] == "run_code"
    assert payload["tool_results"][1]["name"] == "score_task"
    assert payload["tool_results"][1]["result"]["ok"] is False
    assert payload["tool_results"][2]["name"] == "score_task"
    assert payload["tool_results"][2]["result"]["ok"] is True
    assert "model did not complete required score_task step" not in payload["reply"]
    assert "Практическая проверка завершена." in payload["reply"]


def test_practice_code_recovers_after_missing_score_task_without_leaking_auto_error(
    client,
    db_session,
    monkeypatch,
):
    session = _create_code_session(db_session, slug="be-practice-recovery")
    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_code"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_run_code_tool_call("def two_sum(nums, target): return []")]}}]}

        if 1 <= step <= 9:
            assert tool_names == {"score_task"}
            return {"choices": [{"message": {"role": "assistant", "content": "Сейчас сначала соберу комментарий, а потом выставлю оценку."}}]}

        if step == 10:
            assert tool_names == {"score_task"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_score_tool_call(points=2.0, comment=VALID_SCORE_COMMENT)]}}]}

        assert tool_names == set()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Практическая проверка завершена.\n\n"
                            "Корректность: Основная логика пока реализована неполно и проходит только часть кейсов.\n"
                            "Качество кода: Решение выглядит незавершенным и требует более явной структуры.\n"
                            "Сложность и эффективность: Сейчас главный риск связан с корректностью, а не с производительностью.\n"
                            "Что можно улучшить: Доработать алгоритм и отдельно проверить кейсы с дубликатами и отрицательными значениями."
                        ),
                    }
                }
            ]
        }

    def fake_dispatch_tool_call(session_obj, tc, db):
        name = (tc.get("function") or {}).get("name")
        args = json.loads(((tc.get("function") or {}).get("arguments") or "{}"))
        if name == "run_code":
            return _run_code_result(success=False, tests_passed=1)
        return {
            "ok": True,
            "task_id": "C1",
            "points": float(args.get("points") or 0),
            "comment": str(args.get("comment") or ""),
            "is_final": True,
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(practice_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    response = client.post(
        f"/sessions/{session.id}/practice/code",
        json={"task_id": "C1", "language": "python", "code": "def two_sum(nums, target): return []"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_results"][0]["name"] == "run_code"
    assert payload["tool_results"][-1]["name"] == "score_task"
    assert "model did not complete required score_task step" not in payload["reply"]
    assert "Проверка не завершена автоматически." not in payload["reply"]


def test_practice_code_accepts_inline_score_task_recovery_and_returns_final_text(
    client,
    db_session,
    monkeypatch,
):
    session = _create_code_session(db_session, slug="be-practice-inline-recovery")
    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_code"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_run_code_tool_call("def two_sum(nums, target): return []")]}}]}

        if 1 <= step <= 9:
            assert tool_names == {"score_task"}
            return {"choices": [{"message": {"role": "assistant", "content": "Сначала сформулирую комментарий, потом верну tool call."}}]}

        if step == 10:
            assert tool_names == {"score_task"}
            inline_payload = {
                "task_id": "C1",
                "points": 4.0,
                "comment": VALID_SCORE_COMMENT,
            }
            return {"choices": [{"message": {"role": "assistant", "content": f"to=functions.score_task {json.dumps(inline_payload, ensure_ascii=False)}"}}]}

        assert tool_names == set()
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "Практическая проверка завершена.\n\n"
                            "Корректность: Основная логика еще требует доработки, поэтому пока пройдена только часть тестов.\n"
                            "Качество кода: Код короткий, но ему не хватает завершенной рабочей структуры.\n"
                            "Сложность и эффективность: Главный риск сейчас связан с корректностью, а не с асимптотикой.\n"
                            "Что можно улучшить: Собрать рабочий алгоритм и проверить решение на крайних сценариях."
                        ),
                    }
                }
            ]
        }

    def fake_dispatch_tool_call(session_obj, tc, db):
        name = (tc.get("function") or {}).get("name")
        args = json.loads(((tc.get("function") or {}).get("arguments") or "{}"))
        if name == "run_code":
            return _run_code_result(success=False, tests_passed=1)
        return {
            "ok": True,
            "task_id": "C1",
            "points": float(args.get("points") or 0),
            "comment": str(args.get("comment") or ""),
            "is_final": True,
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(practice_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    response = client.post(
        f"/sessions/{session.id}/practice/code",
        json={"task_id": "C1", "language": "python", "code": "def two_sum(nums, target): return []"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_results"][0]["name"] == "run_code"
    assert payload["tool_results"][-1]["name"] == "score_task"
    assert "model did not complete required score_task step" not in payload["reply"]
    assert "Практическая проверка завершена." in payload["reply"]


def test_practice_code_converts_structured_plain_feedback_into_score_task_and_keeps_final_reply(
    client,
    db_session,
    monkeypatch,
):
    session = _create_code_session(db_session, slug="be-practice-plain-feedback")
    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_code"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_run_code_tool_call()]}}]}

        if step == 1:
            assert tool_names == {"score_task"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Оценка: 10/10\n\n" + VALID_SCORE_COMMENT,
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
                            "Практическая проверка завершена.\n\n"
                            "Корректность: Решение получилось рабочим и проходит sandbox-тесты.\n"
                            "Качество кода: Код остается достаточно понятным и не перегружен лишними деталями.\n"
                            "Сложность и эффективность: Для этой задачи текущий подход выглядит достаточно эффективным.\n"
                            "Что можно улучшить: Добавить еще пару собственных тестов на крайние случаи."
                        ),
                    }
                }
            ]
        }

    def fake_dispatch_tool_call(session_obj, tc, db):
        name = (tc.get("function") or {}).get("name")
        args = json.loads(((tc.get("function") or {}).get("arguments") or "{}"))
        if name == "run_code":
            return _run_code_result(success=True, tests_passed=4)
        return {
            "ok": True,
            "task_id": "C1",
            "points": float(args.get("points") or 0),
            "comment": str(args.get("comment") or ""),
            "is_final": True,
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(practice_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    response = client.post(
        f"/sessions/{session.id}/practice/code",
        json={"task_id": "C1", "language": "python", "code": "def two_sum(nums, target): return [0, 1]"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_results"][0]["name"] == "run_code"
    assert payload["tool_results"][-1]["name"] == "score_task"
    assert "Практическая проверка завершена." in payload["reply"]
    assert "Что можно улучшить:" in payload["reply"]


def test_practice_code_persists_model_score_comment_and_retries_theory_like_final_reply(
    client,
    db_session,
    monkeypatch,
):
    session = _create_code_session(db_session, slug="be-practice-final-retry")
    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_code"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_run_code_tool_call()]}}]}

        if 1 <= step <= 12:
            assert tool_names == {"score_task"}
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "Оценка: 10/10. Решение выглядит рабочим, потому что sandbox не показывает падений и базовая логика проходит проверки. "
                                "При этом полезно отдельно проговорить качество кода, сложность и возможные улучшения более структурно."
                            ),
                        }
                    }
                ]
            }

        if step == 13:
            assert tool_names == set()
            return {"choices": [{"message": {"role": "assistant", "content": VALID_SCORE_COMMENT}}]}

        if step == 14:
            assert tool_names == set()
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Теоретический блок завершён, комментарии по каждому ответу, сильные стороны, зоны роста и точная оценка из points.",
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
                            "Практическая проверка завершена.\n\n"
                            "Корректность: Решение прошло sandbox-проверки и на текущем наборе кейсов отрабатывает корректно.\n"
                            "Качество кода: Код читается легко и не перегружен лишними ветвлениями, хотя пару мест можно сделать еще чуть более самодокументируемыми.\n"
                            "Сложность и эффективность: Для этой задачи реализация выглядит достаточно эффективной и не создает заметных рисков по производительности.\n"
                            "Что можно улучшить: Добавить собственные тесты на крайние случаи и при желании чуть яснее назвать промежуточные сущности."
                        ),
                    }
                }
            ]
        }

    def fake_dispatch_tool_call(session_obj, tc, db):
        name = (tc.get("function") or {}).get("name")
        args = json.loads(((tc.get("function") or {}).get("arguments") or "{}"))
        if name == "run_code":
            return _run_code_result(success=True, tests_passed=4)

        comment = str(args.get("comment") or "")
        assert "Корректность:" in comment
        assert "Качество кода:" in comment
        assert "Сложность и эффективность:" in comment
        assert "Что можно улучшить:" in comment
        assert "По результатам прогона явных проблем" not in comment
        db.add(
            models.Score(
                session_id=session_obj.id,
                task_id="C1",
                points=float(args.get("points") or 0),
                comment=comment,
                is_final=True,
            )
        )
        db.flush()
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
        json={"task_id": "C1", "language": "python", "code": "def two_sum(nums, target): return [0, 1]"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_results"][0]["name"] == "run_code"
    assert payload["tool_results"][-1]["name"] == "score_task"
    assert "Теоретический блок завершён" not in payload["reply"]
    assert "сильные стороны" not in payload["reply"].lower()
    assert "зоны роста" not in payload["reply"].lower()
    assert "Корректность:" in payload["reply"]
    assert "Качество кода:" in payload["reply"]
    assert "Что можно улучшить:" in payload["reply"]

    score_row = (
        db_session.query(models.Score)
        .filter_by(session_id=session.id, task_id="C1", is_final=True)
        .order_by(models.Score.id.desc())
        .first()
    )
    assert score_row is not None
    assert "Корректность:" in score_row.comment
    assert "Качество кода:" in score_row.comment
    assert "Сложность и эффективность:" in score_row.comment
    assert "Что можно улучшить:" in score_row.comment
    assert "По результатам прогона явных проблем" not in score_row.comment


def test_practice_code_returns_fallback_reply_when_final_model_call_crashes(
    client,
    db_session,
    monkeypatch,
):
    session = _create_code_session(db_session, slug="be-practice-final-crash")
    state = {"step": 0}

    def fake_chat(messages, tools=None, tool_choice=None):
        step = state["step"]
        state["step"] += 1
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}

        if step == 0:
            assert tool_names == {"run_code"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_run_code_tool_call()]}}]}

        if step == 1:
            assert tool_names == {"score_task"}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [_score_tool_call(points=9.0, comment=VALID_SCORE_COMMENT)]}}]}

        raise RuntimeError("LM final reply crashed")

    def fake_dispatch_tool_call(session_obj, tc, db):
        name = (tc.get("function") or {}).get("name")
        args = json.loads(((tc.get("function") or {}).get("arguments") or "{}"))
        if name == "run_code":
            return _run_code_result(success=True, tests_passed=4)
        return {
            "ok": True,
            "task_id": "C1",
            "points": float(args.get("points") or 0),
            "comment": str(args.get("comment") or ""),
            "is_final": True,
        }

    monkeypatch.setattr(practice_module.lm_client, "chat", fake_chat)
    monkeypatch.setattr(practice_module, "_dispatch_tool_call", fake_dispatch_tool_call)

    response = client.post(
        f"/sessions/{session.id}/practice/code",
        json={"task_id": "C1", "language": "python", "code": "def two_sum(nums, target): return [0, 1]"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_results"][0]["name"] == "run_code"
    assert payload["tool_results"][-1]["name"] == "score_task"
    assert "Практическая проверка завершена." in payload["reply"]
    assert "Корректность:" in payload["reply"]
    assert "Качество кода:" in payload["reply"]
    assert "Что можно улучшить:" in payload["reply"]
