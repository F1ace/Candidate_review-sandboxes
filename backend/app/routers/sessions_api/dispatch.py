import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ... import models
from ...services import sandbox, web_search
from ...services.rag import search_documents
from .state import _get_task_by_id

def _dispatch_tool_call(session: models.Session, tc: dict[str, Any], db: Session) -> dict[str, Any]:
    try:
        function = tc.get("function") or {}
        name = function.get("name")
        raw_args = function.get("arguments") or "{}"
        args = json.loads(raw_args)
        if not isinstance(args, dict):
            args = {}
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
            tests_id = args.get("tests_id")

            if not code:
                return {"ok": False, "error": "code is required"}
            if not language:
                return {"ok": False, "error": "language is required"}

            if not tests_id and task_id:
                task = _get_task_by_id(session.scenario, task_id)
                if task:
                    tests_id = task.get("tests_id")

            return sandbox.run_code(
                language=language,
                code=code,
                tests_id=tests_id,
            )

        if name == "run_sql":
            query = args.get("query") or ""
            if not query:
                return {"ok": False, "error": "query is required"}

            sql_scenario_id = args.get("sql_scenario_id")
            task_id = args.get("task_id")

            if not sql_scenario_id and task_id:
                task = _get_task_by_id(session.scenario, task_id)
                if task:
                    sql_scenario_id = task.get("sql_scenario_id")

            if not sql_scenario_id:
                return {"ok": False, "error": "sql_scenario_id is required"}

            return sandbox.run_sql(
                sql_scenario_id=sql_scenario_id,
                query=query,
            )

        return {"ok": False, "error": f"Unknown tool: {name}"}

    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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
        return {"ok": False, "error": "comment is required and must be non-empty"}

    if task_type == "theory":
        if is_final and not _theory_ready_for_scoring(session, db, task):
            return {"ok": False, "error": "Theory block is not finished yet. Ask all questions first."}

        if points < 1 or points > 10:
            return {"ok": False, "error": "Theory score should be within [1, 10]"}

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

        if is_final:
            current_scores = session.scores or {}
            session.scores = {**current_scores, task_id: points}

        db.commit()
        db.refresh(score)
        return {
            "ok": True,
            "task_id": task_id,
            "points": points,
            "comment": comment,
            "is_final": is_final,
            "question_index": question_index,
        }

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

def _get_theory_intermediate_scores(session: models.Session, db: Session, task_id: str) -> list[models.Score]:
    return (
        db.query(models.Score)
        .filter(
            models.Score.session_id == session.id,
            models.Score.task_id == task_id,
            models.Score.score_type == "theory_intermediate",
        )
        .order_by(models.Score.created_at.asc())
        .all()
    )


def _aggregate_theory_intermediate_scores(session: models.Session, db: Session, task_id: str) -> dict[str, Any]:
    items = _get_theory_intermediate_scores(session, db, task_id)
    if not items:
        return {"count": 0, "avg_points": None, "comments": []}

    avg_points = round(sum(float(x.points) for x in items) / len(items))
    comments = [x.comment.strip() for x in items if (x.comment or "").strip()]

    return {
        "count": len(items),
        "avg_points": avg_points,
        "comments": comments,
    }

