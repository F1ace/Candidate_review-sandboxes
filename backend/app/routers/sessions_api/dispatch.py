import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ... import models
from ...services import sandbox, web_search, sql_runner
from ...services.rag import search_document_chunks
from ...services.theory_rag import (
    find_candidate_answer_message,
    get_existing_validation,
    theory_rag_required,
)
from .state import _get_task_by_id
from .tool_errors import (THEORY_COMMENT_EMPTY, THEORY_COMMENT_TOO_SHORT, THEORY_COMMENT_TRUNCATED, THEORY_COMMENT_TEMPLATE, THEORY_FINAL_TEXTUAL_SCORE, THEORY_FINAL_COMMENTS_REQUIRED, THEORY_FINAL_COMMENTS_TEXTUAL_SCORE,)


def _parse_bool_arg(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    if value is None:
        return default
    return bool(value)


def _resolve_score_task_is_final(
    args: dict[str, Any] | None,
    *,
    task_type: str | None,
    question_index: Any,
) -> bool:
    payload = args if isinstance(args, dict) else {}

    if "is_final" in payload:
        return _parse_bool_arg(payload.get("is_final"), default=True)

    if task_type == "theory":
        return question_index is None

    return True


def _normalize_theory_tool_score_args(
    args: dict[str, Any] | None,
    task: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(args or {})
    if not payload:
        return payload

    if not isinstance(task, dict) or task.get("type") != "theory":
        return payload

    if "points" not in payload:
        return payload

    try:
        requested_points = float(payload.get("points"))
    except Exception:
        return payload

    theory_max_points = int(task.get("max_points", 10) or 10)
    normalized_points = float(max(1, min(theory_max_points, int(round(requested_points)))))
    payload["points"] = normalized_points
    return payload


def _hydrate_internal_final_theory_comments(
    session: models.Session,
    db: Session,
    args: dict[str, Any] | None,
    task: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(args or {})
    if not payload:
        return payload

    if not isinstance(task, dict) or task.get("type") != "theory":
        return payload

    if not _resolve_score_task_is_final(
        payload,
        task_type=task.get("type"),
        question_index=payload.get("question_index"),
    ):
        return payload

    existing_comments = _normalize_theory_final_comments(payload.get("comments"))
    if _validate_final_theory_comments(task, existing_comments) is None:
        return payload

    task_id = (payload.get("task_id") or "").strip()
    if not task_id:
        return payload

    aggregated = _aggregate_theory_intermediate_scores(session, db, task_id)
    fallback_comments = aggregated.get("comments") or []

    if _validate_final_theory_comments(task, fallback_comments) is None:
        payload["comments"] = fallback_comments

    return payload

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
            "description": tc.description,
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
            task_id = args.get("task_id")
            task = _get_task_by_id(session.scenario, task_id) if task_id else None
            args = _normalize_theory_tool_score_args(args, task)
            args = _hydrate_internal_final_theory_comments(session, db, args, task)
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

            task_id = (args.get("task_id") or "").strip()
            question_index = args.get("question_index")

            corpus_id = getattr(session.scenario, "rag_corpus_id", None)
            if not corpus_id:
                return {"ok": False, "error": "RAG corpus is not configured for this scenario"}

            if not task_id:
                return {"ok": False, "error": "task_id is required"}

            if question_index is None:
                return {"ok": False, "error": "question_index is required"}

            task = _get_task_by_id(session.scenario, task_id)
            if not task or task.get("type") != "theory":
                return {"ok": False, "error": f"Task is not a theory task: {task_id}"}

            candidate_message = find_candidate_answer_message(
                session=session,
                db=db,
                task=task,
                question_index=int(question_index),
            )
            if not candidate_message:
                return {
                    "ok": False,
                    "error": f"Candidate has not answered question_index={question_index} yet."
                }

            top_k = int(args.get("top_k") or 5)
            results = search_document_chunks(
                db=db,
                rag_corpus_id=corpus_id,
                query=query,
                top_k=top_k,
            )

            existing = get_existing_validation(
                session_id=session.id,
                task_id=task_id,
                question_index=int(question_index),
                candidate_message_id=candidate_message.id,
                db=db,
            )

            payload = [item.model_dump() for item in results]

            if existing:
                existing.query = query
                existing.status = "completed"
                existing.result_count = len(payload)
                existing.evidence = payload
                db.add(existing)
                db.commit()
                db.refresh(existing)
                validation = existing
            else:
                validation = models.TheoryFactValidation(
                    session_id=session.id,
                    task_id=task_id,
                    question_index=int(question_index),
                    candidate_message_id=candidate_message.id,
                    query=query,
                    status="completed",
                    result_count=len(payload),
                    evidence=payload,
                )
                db.add(validation)
                db.commit()
                db.refresh(validation)

            return {
                "ok": True,
                "task_id": task_id,
                "question_index": int(question_index),
                "validation_saved": True,
                "results": payload,
            }

        if name == "run_code":
            task_id = args.get("task_id")

            current_task = _get_task_by_id(session.scenario, session.current_task_id or "")
            current_task_type = current_task.get("type") if current_task else None
            if current_task_type != "coding":
                return {
                    "ok": False,
                    "task_id": task_id,
                    "error": "run_code is not available in the current block",
                }

            language = (args.get("language") or "").strip()
            code = args.get("code") or ""

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
            current_task = _get_task_by_id(session.scenario, session.current_task_id or "")
            current_task_type = current_task.get("type") if current_task else None
            if current_task_type != "sql":
                return {
                    "ok": False,
                    "error": "run_sql is not available in the current block",
                }

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
    question_index = args.get("question_index")
    is_final = _resolve_score_task_is_final(
        args,
        task_type=task_type,
        question_index=question_index,
    )

    try:
        points = float(args.get("points", 0))
    except Exception:
        points = 0.0

    points = float(int(round(points)))
    comment = (args.get("comment") or "").strip()
    final_comments = _normalize_theory_final_comments(args.get("comments"))
    if not comment:
        return {"ok": False, "error": THEORY_COMMENT_EMPTY}

    if task_type == "theory":
        if len(comment) < 30:
            return {"ok": False, "error": THEORY_COMMENT_TOO_SHORT}

        trimmed = comment.rstrip()
        if trimmed.endswith(("—", "-", ":", ",", ";")):
            return {"ok": False, "error": THEORY_COMMENT_TRUNCATED}

        if is_final:
            final_comment_error = _validate_final_theory_comment(comment)
            if final_comment_error:
                return {"ok": False, "error": final_comment_error}

            final_comments_error = _validate_final_theory_comments(task, final_comments)
            if final_comments_error:
                return {"ok": False, "error": final_comments_error}

        tail = comment[-12:].strip().lower()
        suspicious_tail = (
            len(comment.split()[-1]) <= 2
            or tail in {"и", "а", "но", "что", "как", "к", "по", "в", "на", "с"}
            or re.search(r"\b[а-яa-z]{1,2}$", comment.lower()) is not None
        )

    if task_type in {"coding", "sql"}:
        comment_error = _validate_practice_comment(comment, task_type)
        if comment_error:
            return {"ok": False, "error": comment_error}

    if task_type == "theory":
        theory_max_points = int(task.get("max_points", 10) or 10)
        if points < 1 or points > theory_max_points:
            return {"ok": False, "error": f"Theory score should be within [1, {theory_max_points}]"}

        template_error = _validate_theory_comment_not_template(comment)
        if template_error:
            return {"ok": False, "error": template_error}

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

            if theory_rag_required(session, db):
                candidate_message = find_candidate_answer_message(session, db, task, question_index)
                if not candidate_message:
                    return {
                        "ok": False,
                        "error": f"Candidate has not answered question_index={question_index} yet.",
                    }

                validation = get_existing_validation(
                    session_id=session.id,
                    task_id=task_id,
                    question_index=question_index,
                    candidate_message_id=candidate_message.id,
                    db=db,
                )
                if not validation:
                    return {
                        "ok": False,
                        "error_code": "theory_rag_validation_required",
                        "error": (
                            "Theory answer must be validated against scenario documents before scoring. "
                            "Run rag_search first."
                        ),
                    }
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

            points = _compute_final_theory_points(
                points,
                aggregated["avg_points"],
                theory_max_points,
            )
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
            "is_final": is_final,
            "question_index": question_index,
        }


        response["comment"] = comment

        if is_final:
            current_scores = session.scores or {}
            session.scores = {**current_scores, task_id: points}
            response["avg_points"] = aggregated["avg_points"]
            response["aggregated"] = aggregated
            response["comments"] = final_comments

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
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
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
        rf"(?im)(?:^|[\n\r]|[.!?]\s+)\s*[*_`\->#\s]*\s*вопрос\s*{question_index}\s*/\s*{total}"
        rf"(?:\s*[\(\[].*?[\)\]])?"
        rf"\s*[:\-—]\s*"
    )

    history = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
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

def _validate_theory_comment_structure(comment: str) -> str | None:
    required_sections = ["верно:", "не хватает:", "ошибка/сомнение:"]
    low = (comment or "").lower()
    missing = [section for section in required_sections if section not in low]
    if missing:
        return f"Theory comment must contain sections: {', '.join(required_sections)}"
    return None

def _validate_theory_comment_not_template(comment: str) -> str | None:
    raw = (comment or "").strip()
    low = raw.lower()

    normalized = re.sub(r"[\s:;/,.\-–—]+", " ", low).strip()

    template_variants = {
        "верно не хватает ошибка сомнение",
        "верно не хватает ошибка",
        "корректно не хватает ошибка",
        "что верно чего не хватает ошибка сомнение",
    }

    if normalized in template_variants:
        return THEORY_COMMENT_TEMPLATE

    markers = ["верно", "не хватает", "ошибка"]
    has_all_markers = all(marker in low for marker in markers)

    if has_all_markers and len(raw) < 80:
        return THEORY_COMMENT_TEMPLATE

    if re.fullmatch(
        r"(верно|корректно)\s*[/|,;\-]?\s*не\s*хватает\s*[/|,;\-]?\s*ошибка(?:\s*/\s*сомнение)?",
        low
    ):
        return THEORY_COMMENT_TEMPLATE

    return None

def _normalize_theory_final_comments(raw_comments: Any) -> list[str]:
    if not isinstance(raw_comments, list):
        return []
    return [str(item).strip() for item in raw_comments if str(item).strip()]


def _validate_final_theory_comments(task: dict[str, Any], comments: list[str]) -> str | None:
    question_count = _theory_question_count(task)
    if question_count <= 0:
        return None

    if len(comments) != question_count:
        return THEORY_FINAL_COMMENTS_REQUIRED

    forbidden_patterns = [
        r"\b\d+\s*/\s*10\b",
        r"\b\d+\s+из\s+10\b",
        r"\bоценка\s*[:\-]?\s*\d+\b",
        r"\b\d+\s+балл",
    ]
    for item in comments:
        low = (item or "").lower()
        if not low:
            return THEORY_FINAL_COMMENTS_REQUIRED
        if any(re.search(pattern, low) for pattern in forbidden_patterns):
            return THEORY_FINAL_COMMENTS_TEXTUAL_SCORE

    return None

def _validate_final_theory_comment(comment: str) -> str | None:
    low = (comment or "").lower()

    forbidden_patterns = [
        r"\b\d+\s*/\s*10\b",
        r"\bставлю\s+\d+\b",
        r"\bоценка\s*[:\-]?\s*\d+\b",
        r"\b\d+\s+из\s+10\b",
        r"\bзаслуживает\s+\d+\b",
        r"\bитоговая\s+оценка\s*[:\-]?\s*\d+\b",
    ]

    for pattern in forbidden_patterns:
        if re.search(pattern, low):
            return THEORY_FINAL_TEXTUAL_SCORE

    return None

def _compute_final_theory_points(
    requested_points: float,
    aggregated_avg: int,
    max_points: int,
) -> float:
    normalized_requested = int(round(requested_points))
    normalized_requested = max(1, min(max_points, normalized_requested))

    normalized_avg = int(round(aggregated_avg))
    normalized_avg = max(1, min(max_points, normalized_avg))

    # Финальный theory score не должен завышать уже рассчитанное среднее
    # по промежуточным оценкам, но может быть ниже этого среднего.
    return float(min(normalized_requested, normalized_avg))

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
    if not comments:
        return aggregated_avg

    weak_count = 0
    error_count = 0
    critical_count = 0

    no_error_markers = {
        "",
        "нет",
        "нет.",
        "отсутствует",
        "отсутствует.",
        "ошибок нет",
        "сомнений нет",
        "явных ошибок нет",
        "существенных ошибок нет",
    }

    for comment in comments or []:
        low = (comment or "").lower()

        if "не хватает:" in low:
            missing_part = low.split("не хватает:", 1)[1].split("ошибка/сомнение:", 1)[0].strip()
            if missing_part and missing_part not in {"нет", "нет.", "ничего", "ничего существенного"}:
                weak_count += 1

        if "ошибка/сомнение:" in low:
            error_part = low.split("ошибка/сомнение:", 1)[1].strip()
            if error_part not in no_error_markers:
                if any(marker in error_part for marker in [
                    "критическая ошибка",
                    "неверно раскрыта суть",
                    "ошибка в базовом определении",
                    "перепутано с",
                ]):
                    critical_count += 1
                else:
                    error_count += 1

    penalty = 0

    # Неполнота по нескольким вопросам — мягкий штраф
    if weak_count >= 2:
        penalty += 1

    # Обычные ошибки не должны обрушать итог, если промежуточные баллы уже это учли
    if error_count >= 2:
        penalty += 1

    # Критическая ошибка — ещё один штраф
    if critical_count >= 1:
        penalty += 1

    penalty = min(penalty, 2)
    return max(1, aggregated_avg - penalty)
