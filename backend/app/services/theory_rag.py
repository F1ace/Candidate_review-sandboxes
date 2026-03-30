from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from .rag import search_document_chunks


def theory_rag_required(session: models.Session, db: Session) -> bool:
    corpus_id = getattr(session.scenario, "rag_corpus_id", None)
    if not corpus_id:
        return False
    return (
        db.query(models.Document)
        .filter(
            models.Document.rag_corpus_id == corpus_id,
            models.Document.status == "ready",
        )
        .count()
        > 0
    )


def _question_text(task: dict[str, Any], question_index: int) -> str:
    questions = task.get("questions") or []
    if question_index < 1 or question_index > len(questions):
        return ""
    question = questions[question_index - 1]
    if isinstance(question, dict):
        return (
            question.get("text")
            or question.get("question")
            or question.get("prompt")
            or ""
        ).strip()
    return str(question).strip()


def _question_pattern(question_index: int, total: int) -> re.Pattern[str]:
    return re.compile(
        rf"(?im)(?:^|\n)\s*[*_`\->#\s]*\s*вопрос\s*{question_index}\s*/\s*{total}"
        rf"(?:\s*[\(\[].*?[\)\]])?\s*[:\-—]\s*"
    )


def detect_current_theory_question_index(
    session: models.Session,
    db: Session,
    task: dict[str, Any],
) -> int | None:
    questions = task.get("questions") or []
    total = len(questions)
    if not total:
        return None

    history = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at.desc(), models.Message.id.desc())
        .all()
    )

    for message in history:
        if message.sender != "model":
            continue
        text = (message.text or "").strip()
        match = re.search(r"(?im)вопрос\s+(\d+)\s*/\s*(\d+)", text)
        if not match:
            continue
        idx = int(match.group(1))
        total_in_message = int(match.group(2))
        if total_in_message == total and 1 <= idx <= total:
            return idx

    return None


def find_candidate_answer_message(
    session: models.Session,
    db: Session,
    task: dict[str, Any],
    question_index: int,
) -> models.Message | None:
    questions = task.get("questions") or []
    total = len(questions)
    if not total:
        return None

    history = (
        db.query(models.Message)
        .filter_by(session_id=session.id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .all()
    )

    last_q_idx = None
    pattern = _question_pattern(question_index, total)
    for idx, message in enumerate(history):
        if message.sender != "model":
            continue
        if pattern.search((message.text or "").strip()):
            last_q_idx = idx

    if last_q_idx is None:
        return None

    for message in history[last_q_idx + 1 :]:
        if message.sender == "candidate" and (message.text or "").strip():
            return message
        if message.sender == "model" and (message.text or "").strip():
            break

    return None


def build_theory_validation_query(task: dict[str, Any], question_index: int, answer_text: str) -> str:
    question_text = _question_text(task, question_index)
    return (
        f"Теоретический вопрос: {question_text}\n"
        f"Ответ кандидата: {answer_text.strip()}\n"
        "Найди в документах фрагменты, которыми можно проверить факты из ответа."
    ).strip()


def get_existing_validation(
    *,
    session_id: str,
    task_id: str,
    question_index: int,
    candidate_message_id: int,
    db: Session,
) -> models.TheoryFactValidation | None:
    return (
        db.query(models.TheoryFactValidation)
        .filter(
            models.TheoryFactValidation.session_id == session_id,
            models.TheoryFactValidation.task_id == task_id,
            models.TheoryFactValidation.question_index == question_index,
            models.TheoryFactValidation.candidate_message_id == candidate_message_id,
        )
        .order_by(models.TheoryFactValidation.created_at.desc(), models.TheoryFactValidation.id.desc())
        .first()
    )


def ensure_theory_validation(
    *,
    session: models.Session,
    db: Session,
    task: dict[str, Any],
    question_index: int,
) -> models.TheoryFactValidation | None:
    if not theory_rag_required(session, db):
        return None

    task_id = task.get("id")
    if not task_id:
        return None

    candidate_message = find_candidate_answer_message(session, db, task, question_index)
    if not candidate_message:
        return None

    existing = get_existing_validation(
        session_id=session.id,
        task_id=task_id,
        question_index=question_index,
        candidate_message_id=candidate_message.id,
        db=db,
    )
    if existing:
        return existing

    query = build_theory_validation_query(task, question_index, candidate_message.text or "")
    results = search_document_chunks(
        db=db,
        rag_corpus_id=int(session.scenario.rag_corpus_id),
        query=query,
        top_k=settings.rag_default_top_k,
    )
    validation = models.TheoryFactValidation(
        session_id=session.id,
        task_id=task_id,
        question_index=question_index,
        candidate_message_id=candidate_message.id,
        query=query,
        status="completed",
        result_count=len(results),
        evidence=[item.model_dump() for item in results],
    )
    db.add(validation)
    db.commit()
    db.refresh(validation)
    return validation


def format_theory_validation_message(validation: models.TheoryFactValidation | None) -> str:
    if not validation:
        return ""

    evidence = validation.evidence or []
    if not evidence:
        return (
            "<THEORY_RAG_EVIDENCE>\n"
            "По документам сценария совпадения не найдены. Это тоже часть проверки: если ответ не подтверждается корпусом, учитывай это при оценке.\n"
            "</THEORY_RAG_EVIDENCE>"
        )

    lines = [
        "<THEORY_RAG_EVIDENCE>",
        "Ниже фрагменты документов сценария, которые нужно использовать при оценке ответа кандидата.",
    ]
    for idx, item in enumerate(evidence, start=1):
        filename = item.get("filename") or "document"
        snippet = (item.get("snippet") or "").strip()
        metadata = item.get("metadata") or {}
        chunk_index = metadata.get("chunk_index")
        score = item.get("score")
        lines.append(
            f"{idx}. Файл: {filename}; chunk={chunk_index}; score={score}; snippet={snippet}"
        )
    lines.append(
        "Перед промежуточной оценкой опирайся на эти фрагменты и явно учитывай, подтверждается ли ответ кандидата корпусом."
    )
    lines.append("</THEORY_RAG_EVIDENCE>")
    return "\n".join(lines)
