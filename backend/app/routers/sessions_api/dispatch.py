import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ... import models
from ...services import sandbox, web_search, sql_runner
from .state import _get_task_by_id
from .tool_errors import (THEORY_COMMENT_EMPTY, THEORY_COMMENT_TOO_SHORT, THEORY_COMMENT_TRUNCATED,)

def _build_tests_payload(task: models.Task) -> list[dict[str, Any]]:
    extra = task.extra_config or {}

    # Фолбэк на старые/альтернативные ключи
    entrypoint_kind = (
        extra.get("entrypoint_kind")
        or extra.get("entrypointKind")
    )
    entrypoint_name = (
        extra.get("entrypoint_name")
        or extra.get("entrypoint")
    )
    method_name = extra.get("method_name")

    # Дополнительный фолбэк: если extra_config неполный, пробуем взять из JSON сценария
    scenario_task = None
    if getattr(task, "scenario", None) and getattr(task.scenario, "tasks", None):
        for item in task.scenario.tasks or []:
            if item.get("id") == task.external_id:
                scenario_task = item
                break

    if scenario_task:
        entrypoint_kind = entrypoint_kind or scenario_task.get("entrypoint_kind")
        entrypoint_name = entrypoint_name or scenario_task.get("entrypoint")
        method_name = method_name or scenario_task.get("method_name")

    result = []

    for tc in task.test_cases:
        if not tc.is_active:
            continue

        input_data = dict(tc.input_data or {})
        expected_error = input_data.pop("__expected_error__", None)

        result.append({
            "code": tc.code,
            "name": tc.name,
            "language": tc.language,
            "input_data": input_data,
            "expected_output": tc.expected_output,
            "checker_source": tc.checker_source,
            "expected_error": expected_error,
            "entrypoint_kind": entrypoint_kind,
            "entrypoint_name": entrypoint_name,
            "method_name": method_name,
        })

    return result

def normalize_sandbox_result(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "exit_code": 1,
            "details": "sandbox returned non-dict result",
            "tests_total": 0,
            "tests_passed": 0,
            "test_results": [],
        }

    test_results = raw.get("test_results") or []
    tests_total = raw.get("tests_total")
    if tests_total is None:
        tests_total = len(test_results)

    tests_passed = raw.get("tests_passed")
    if tests_passed is None:
        tests_passed = sum(1 for item in test_results if item.get("passed"))

    success = raw.get("success")
    if success is None:
        success = tests_total > 0 and tests_passed == tests_total

    return {
        "success": bool(success),
        "stdout": raw.get("stdout") or "",
        "stderr": raw.get("stderr") or "",
        "exit_code": raw.get("exit_code", 0),
        "details": raw.get("details"),
        "tests_total": int(tests_total or 0),
        "tests_passed": int(tests_passed or 0),
        "test_results": test_results,
    }

def _dispatch_tool_call(session: models.Session, tc: dict[str, Any], db: Session) -> dict[str, Any]:
    try:
        function = tc.get("function") or {}
        name = function.get("name")
        raw_args = function.get("arguments") or "{}"
        args = json.loads(raw_args)
        if not isinstance(args, dict):
            args = {}

        if isinstance(name, str) and "." in name:
            name = name.split(".")[-1]

        if name == "functions":
            nested_name = args.get("name")
            nested_args = args.get("arguments")

            if isinstance(nested_name, str):
                name = nested_name.split(".")[-1]

            if isinstance(nested_args, str):
                try:
                    parsed_nested_args = json.loads(nested_args)
                    if isinstance(parsed_nested_args, dict):
                        args = parsed_nested_args
                except Exception:
                    pass
            elif isinstance(nested_args, dict):
                args = nested_args

    except Exception as exc:
        return {"ok": False, "error": f"Invalid tool call payload: {exc}"}

    try:
        if name == "score_task":
            return _apply_score(session, args, db)

        if name == "web_search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"ok": False, "error": "query is required"}
            top_k = int(args.get("top_k") or 5)
            return {
                "ok": True,
                "results": web_search.web_search(query=query, top_k=top_k),
            }

        if name == "rag_search":
            query = (args.get("query") or "").strip()
            if not query:
                return {"ok": False, "error": "query is required"}

            corpus_id = getattr(session.scenario, "rag_corpus_id", None)
            if not corpus_id:
                return {"ok": False, "error": "RAG corpus is not configured for this scenario"}

            top_k = int(args.get("top_k") or 5)
            return {
                "ok": True,
                "results": search_documents(
                    db=db,
                    rag_corpus_id=corpus_id,
                    query=query,
                    top_k=top_k,
                ),
            }

        if name == "run_code":
            language = (args.get("language") or "").strip()
            code = args.get("code") or ""
            task_id = args.get("task_id")

            if not code:
                return {"ok": False, "task_id": task_id, "error": "code is required"}
            if not language:
                return {"ok": False, "task_id": task_id, "error": "language is required"}
            if not task_id:
                return {"ok": False, "task_id": task_id, "error": "task_id is required"}

            task_row = (
                db.query(models.Task)
                .filter(
                    models.Task.scenario_id == session.scenario_id,
                    models.Task.external_id == task_id,
                )
                .first()
            )

            if not task_row:
                return {"ok": False, "task_id": task_id, "error": f"Task not found: {task_id}"}

            if task_row.task_type != "coding":
                return {"ok": False, "task_id": task_id, "error": f"Task is not a coding task: {task_id}"}

            tests_payload = _build_tests_payload(task_row)
            if not tests_payload:
                return {
                    "ok": False,
                    "task_id": task_id,
                    "error": f"No active testcases linked to task: {task_id}",
                }

            raw_result = sandbox.run_code(
                language=language,
                code=code,
                tests=tests_payload,
            )

            return {
                "ok": True,
                "task_id": task_id,
                "result": normalize_sandbox_result(raw_result),
            }

        if name == "run_sql":
            query = (args.get("query") or "").strip()
            if not query:
                return {"ok": False, "error": "query is required"}

            task_id = args.get("task_id")
            sql_scenario_id = (args.get("sql_scenario_id") or "").strip()

            task_row = None
            if task_id:
                task_row = (
                    db.query(models.Task)
                    .filter(
                        models.Task.scenario_id == session.scenario_id,
                        models.Task.external_id == task_id,
                    )
                    .first()
                )
                if not task_row:
                    return {"ok": False, "task_id": task_id, "error": f"Task not found: {task_id}"}

                if task_row.task_type != "sql":
                    return {"ok": False, "task_id": task_id, "error": f"Task is not a SQL task: {task_id}"}

                if not sql_scenario_id:
                    sql_scenario_id = (task_row.sql_scenario_ref or "").strip()

            if task_row:
                return sql_runner.run_sql_for_task(
                    db=db,
                    task_row=task_row,
                    query=query,
                )

            if not sql_scenario_id:
                return {"ok": False, "error": "sql_scenario_id is required"}

            return sql_runner.run_sql_for_scenario_name(
                db=db,
                scenario_name=sql_scenario_id,
                query=query,
            )

        return {"ok": False, "error": f"Unknown tool: {name}"}

    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

def _validate_practice_comment(comment: str, task_type: str) -> str | None:
    if task_type == "sql":
        required_headers = [
            "Корректность:",
            "Качество решения:",
            "Работа с SQL:",
            "Что можно улучшить:",
        ]
    else:
        # coding остаётся как раньше
        required_headers = [
            "Корректность:",
            "Качество кода:",
            "Сложность и эффективность:",
            "Что можно улучшить:",
        ]

    missing = [header for header in required_headers if header not in comment]
    if missing:
        return "Practice comment does not match required template. Missing sections: " + ", ".join(missing)

    lowered = comment.lower()
    forbidden_markers = [
        "[",
        "]",
        "заполни",
        "если применимо",
        "1-3 конкретных замечания",
    ]
    if any(marker in lowered for marker in forbidden_markers):
        return "Practice comment contains placeholders or template instructions instead of final feedback."

    lines = [line.strip() for line in comment.splitlines() if line.strip()]
    section_values: dict[str, str] = {}

    for line in lines:
        for header in required_headers:
            if line.startswith(header):
                value = line[len(header):].strip()
                section_values[header] = value
                break

    empty_sections = [header for header in required_headers if not section_values.get(header)]
    if empty_sections:
        return "Practice comment has empty sections: " + ", ".join(empty_sections)

    return None

def _apply_score(session: models.Session, args: dict[str, Any], db: Session) -> dict[str, Any]:
    task_id = args.get("task_id")
    task = _get_task_by_id(session.scenario, task_id)
    if not task:
        return {"ok": False, "error": f"Task {task_id} not found in scenario"}

    task_type = task.get("type")
    max_points = int(task.get("max_points", 0) or 0)
    is_final = bool(args.get("is_final", True))
    question_index = args.get("question_index")

    try:
        points = float(args.get("points", 0))
    except Exception:
        points = 0.0

    points = float(int(round(points)))
    comment = (args.get("comment") or "").strip()
    if not comment:
        return {"ok": False, "error": THEORY_COMMENT_EMPTY}

    if task_type == "theory":
        if len(comment) < 45:
            return {"ok": False, "error": THEORY_COMMENT_TOO_SHORT,}
        
        trimmed = comment.rstrip()
        if trimmed.endswith(("—", "-", ":", ",", ";")):
            return {"ok": False, "error": THEORY_COMMENT_TRUNCATED}

        # Похоже на оборванный комментарий: заканчивается на служебный обрывок
        # или на очень короткий хвост без знака завершения.
        tail = comment[-12:].strip().lower()
        suspicious_tail = (
            len(comment.split()[-1]) <= 2
            or tail in {"и", "а", "но", "что", "как", "к", "по", "в", "на", "с"}
            or re.search(r"\b[а-яa-z]{1,2}$", comment.lower()) is not None
        )

        has_terminal_punctuation = bool(re.search(r"[.!?…]\s*$", comment))

        # Если нет завершающего знака и при этом хвост выглядит оборванным — режем.
        if suspicious_tail and not has_terminal_punctuation:
            return {
                "ok": False,
                "error": THEORY_COMMENT_TRUNCATED,
            }
    if task_type in {"coding", "sql"}:
        comment_error = _validate_practice_comment(comment, task_type)
        if comment_error:
            return {"ok": False, "error": comment_error}

    if task_type == "theory":
        if points < 1 or points > 10:
            return {"ok": False, "error": "Theory score should be within [1, 10]"}

        if not is_final:
            validation_error = _validate_theory_intermediate_score_args(task, question_index)
            if validation_error:
                return {"ok": False, "error": validation_error}
            question_index = int(question_index)

            readiness_error = _theory_intermediate_ready_for_scoring(
                session,
                db,
                task,
                question_index,
            )
            if readiness_error:
                return {"ok": False, "error": readiness_error}
        else:
            if not _theory_ready_for_scoring(session, db, task):
                return {"ok": False, "error": "Theory block is not finished yet. Ask all questions first."}

            aggregated = _aggregate_theory_intermediate_scores(session, db, task_id)

            if aggregated["expected_questions"] and aggregated["missing_questions"]:
                return {
                    "ok": False,
                    "error": (
                        "Theory intermediate scores are incomplete. "
                        f"Missing question_index: {aggregated['missing_questions']}"
                    ),
                }

            if aggregated["avg_points"] is None:
                return {"ok": False, "error": "Theory final score requires intermediate scores first."}

            requested_points = points
            penalized_avg = _apply_theory_penalties(
                aggregated["avg_points"],
                aggregated["comments"],
            )
            points = _compute_final_theory_points(requested_points, penalized_avg)
            question_index = None

        score = models.Score(
            session_id=session.id,
            task_id=task_id,
            points=points,
            comment=comment,
            is_final=is_final,
            question_index=question_index,
            score_type="theory_final" if is_final else "theory_intermediate",
        )
        db.add(score)

        response: dict[str, Any] = {
            "ok": True,
            "task_id": task_id,
            "points": points,
            "comment": comment,
            "is_final": is_final,
            "question_index": question_index,
        }

        if is_final:
            current_scores = session.scores or {}
            session.scores = {**current_scores, task_id: points}
            response["aggregated"] = aggregated

        db.commit()
        db.refresh(score)
        return response

    if points < 0 or points > max_points:
        return {"ok": False, "error": f"Points should be within [0, {max_points}]"}

    score = models.Score(
        session_id=session.id,
        task_id=task_id,
        points=points,
        comment=comment,
        is_final=True,
        question_index=None,
        score_type="practice",
    )
    current_scores = session.scores or {}
    session.scores = {**current_scores, task_id: points}
    db.add(score)
    db.commit()
    db.refresh(score)

    return {
        "ok": True,
        "task_id": task_id,
        "points": points,
        "comment": comment,
        "is_final": True,
    }

def _theory_ready_for_scoring(session: models.Session, db: Session, task: dict) -> bool:
    questions = task.get("questions") or []
    if not questions:
        return True

    n = len(questions)

    last_q_re = re.compile(
        # Разрешаем: "**Вопрос 4/4:**", "Вопрос 4/4 (T-REST):", "- Вопрос 4/4 [любой текст]:"
        rf"^\s*[*_`\->#\s]*\s*вопрос\s*{n}\s*/\s*{n}"
        rf"(?:\s*[\(\[].*?[\)\]])?"   # необязательная приписка в () или [] — например (T-REST)
        rf"\s*[:\-—]\s*",
        re.IGNORECASE,
    )

    history = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at)
        .all()
    )

    last_q_idx = None
    for i, m in enumerate(history):
        if m.sender != "model":
            continue
        txt = (m.text or "").strip()
        if last_q_re.match(txt):
            last_q_idx = i

    if last_q_idx is None:
        return False

    # После последнего вопроса должен быть хотя бы один осмысленный ответ кандидата
    for m in history[last_q_idx + 1 :]:
        if m.sender == "candidate" and (m.text or "").strip():
            return True

    return False

def _theory_intermediate_ready_for_scoring(
    session: models.Session,
    db: Session,
    task: dict,
    question_index: int,
) -> str | None:
    questions = task.get("questions") or []
    if not questions:
        return "Theory task has no questions configured."

    total = len(questions)

    existing = (
        db.query(models.Score)
        .filter(
            models.Score.session_id == session.id,
            models.Score.task_id == task.get("id"),
            models.Score.is_final.is_(False),
            models.Score.question_index == question_index,
        )
        .first()
    )
    if existing:
        return f"Intermediate score for question_index={question_index} already exists."

    q_re = re.compile(
        rf"(?im)(?:^|\n)\s*[*_`\->#\s]*\s*вопрос\s*{question_index}\s*/\s*{total}"
        rf"(?:\s*[\(\[].*?[\)\]])?"
        rf"\s*[:\-—]\s*"
    )

    history = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at)
        .all()
    )

    last_q_idx = None
    for i, m in enumerate(history):
        if m.sender != "model":
            continue
        txt = (m.text or "").strip()
        if q_re.search(txt):
            last_q_idx = i

    if last_q_idx is None:
        return f"Question {question_index}/{total} has not been asked yet."

    for m in history[last_q_idx + 1:]:
        if m.sender == "candidate" and (m.text or "").strip():
            return None
        if m.sender == "model" and (m.text or "").strip():
            break

    return f"Candidate has not answered question_index={question_index} yet."

def _get_theory_intermediate_scores(session: models.Session, db: Session, task_id: str) -> list[models.Score]:
    return (
        db.query(models.Score)
        .filter(
            models.Score.session_id == session.id,
            models.Score.task_id == task_id,
            models.Score.score_type == "theory_intermediate",
        )
        .order_by(models.Score.created_at.asc(), models.Score.id.asc())
        .all()
    )


def _theory_question_count(task: dict[str, Any]) -> int:
    return len(task.get("questions") or [])

def _latest_intermediate_scores_by_question(
    session: models.Session,
    db: Session,
    task_id: str,
) -> dict[int, models.Score]:
    latest: dict[int, models.Score] = {}
    for item in _get_theory_intermediate_scores(session, db, task_id):
        qidx = item.question_index
        if isinstance(qidx, int):
            latest[qidx] = item
    return latest

def _validate_theory_intermediate_score_args(task: dict[str, Any], question_index: Any) -> str | None:
    question_count = _theory_question_count(task)
    if question_count <= 0:
        return None
    if question_index is None:
        return "question_index is required for theory intermediate score"
    try:
        idx = int(question_index)
    except Exception:
        return "question_index must be an integer"
    if idx < 1 or idx > question_count:
        return f"question_index must be within [1, {question_count}]"
    return None

def _compute_final_theory_points(
    requested_points: float,
    aggregated_avg: int,
) -> float:
    # Финальная оценка не должна быть выше усреднённого промежуточного балла.
    # Допускаем только понижение, а не повышение.
    capped = min(int(round(requested_points)), int(round(aggregated_avg)))
    return float(max(1, min(10, capped)))

def _aggregate_theory_intermediate_scores(session: models.Session, db: Session, task_id: str) -> dict[str, Any]:
    task = _get_task_by_id(session.scenario, task_id) or {}
    latest = _latest_intermediate_scores_by_question(session, db, task_id)
    expected_questions = _theory_question_count(task)
    comments = [
        latest[idx].comment.strip()
        for idx in sorted(latest)
        if (latest[idx].comment or "").strip()
    ]

    if not latest:
        return {
            "count": 0,
            "avg_points": None,
            "comments": comments,
            "expected_questions": expected_questions,
            "scored_questions": [],
            "missing_questions": list(range(1, expected_questions + 1)) if expected_questions else [],
        }

    avg_points = round(sum(float(x.points) for x in latest.values()) / len(latest))
    missing = [idx for idx in range(1, expected_questions + 1) if idx not in latest] if expected_questions else []

    return {
        "count": len(latest),
        "avg_points": avg_points,
        "comments": comments,
        "expected_questions": expected_questions,
        "scored_questions": sorted(latest),
        "missing_questions": missing,
    }

def _apply_theory_penalties(
    aggregated_avg: int,
    comments: list[str],
) -> int:
    penalty = 0
    joined = " ".join((comments or [])).lower()

    weak_markers = [
        "не привёл пример",
        "не привел пример",
        "без примера",
        "не все статусы",
        "неполный ответ",
        "ответ фрагментарный",
        "есть пробелы",
        "не хватает деталей",
        "неполное объяснение",
    ]

    for marker in weak_markers:
        if marker in joined:
            penalty += 1

    # Не даём штрафу уйти слишком далеко
    penalty = min(penalty, 2)

    return max(1, aggregated_avg - penalty)
