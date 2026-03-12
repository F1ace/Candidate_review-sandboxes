from datetime import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from ... import models, schemas
from ...database import get_db
from ...services import sandbox, web_search
from .practice import _practice_agent_review
from .router import router
from .schemas import PracticeCodeRequest, PracticeSqlRequest
from .state import _get_task_by_id
from .state import advance_task_if_needed
@router.post("/", response_model=schemas.SessionOut, status_code=status.HTTP_201_CREATED)
@router.post("", response_model=schemas.SessionOut, status_code=status.HTTP_201_CREATED)
def create_session(payload: schemas.SessionCreate, db: Session = Depends(get_db)):
    scenario = db.get(models.Scenario, payload.scenario_id)
    role = db.get(models.Role, payload.role_id)
    if not scenario or not role:
        raise HTTPException(status_code=400, detail="Scenario or role not found")
    if scenario.role_id != role.id:
        raise HTTPException(status_code=400, detail="Scenario does not belong to the selected role")
    session = models.Session(
        scenario_id=payload.scenario_id,
        role_id=payload.role_id,
        candidate_id=payload.candidate_id,
        state="active",
        current_task_id=None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session

@router.get("/{session_id}", response_model=schemas.SessionOut)
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session

@router.get("/{session_id}/messages", response_model=list[schemas.MessageOut])
def list_messages(session_id: str, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return db.query(models.Message).filter_by(session_id=session_id).order_by(models.Message.created_at).all()

@router.post("/{session_id}/messages", response_model=schemas.MessageOut)
def post_message(session_id: str, payload: schemas.MessageCreate, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    message = models.Message(session_id=session_id, **payload.model_dump())
    db.add(message)
    # <-- ВАЖНО: если кандидат написал "Следующее", пробуем перевести задачу
    if payload.sender == "candidate":
        if advance_task_if_needed(session, payload.text):
            # можно добавить системное сообщение для ясности
            db.add(models.Message(
                session_id=session_id,
                sender="system",
                text=f"Переход к следующему заданию: {session.current_task_id}",
                task_id=session.current_task_id,
            ))
    db.commit()
    db.refresh(message)
    return message

@router.post("/{session_id}/score", response_model=schemas.ScoreOut)
def score_task(session_id: str, payload: schemas.ScoreCreate, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    scenario = session.scenario
    task = _get_task_by_id(scenario, payload.task_id)
    if not task:
        raise HTTPException(status_code=400, detail="Task not found in scenario")

    task_type = task.get("type")
    max_points = int(task.get("max_points", 0) or 0)
    points = float(int(round(float(payload.points))))
    comment = (payload.comment or "").strip()

    if not comment:
        raise HTTPException(status_code=400, detail="comment is required and must be non-empty")

    if task_type == "theory":
        if points < 1 or points > 10:
            raise HTTPException(status_code=400, detail="Theory score should be within [1, 10]")

        score = models.Score(
            session_id=session_id,
            task_id=payload.task_id,
            points=points,
            comment=comment,
            is_final=payload.is_final,
            question_index=payload.question_index,
            score_type="theory_final" if payload.is_final else "theory_intermediate",
        )
        db.add(score)

        if payload.is_final:
            current_scores = session.scores or {}
            session.scores = {**current_scores, payload.task_id: points}

        db.commit()
        db.refresh(score)
        return score

    if points < 0 or points > max_points:
        raise HTTPException(
            status_code=400,
            detail=f"Points should be within [0, {max_points}]",
        )

    score = models.Score(
        session_id=session_id,
        task_id=payload.task_id,
        points=points,
        comment=comment,
        is_final=True,
        question_index=None,
        score_type="practice",
    )

    current_scores = session.scores or {}
    session.scores = {**current_scores, payload.task_id: points}

    db.add(score)
    db.commit()
    db.refresh(score)
    return score

@router.post("/{session_id}/tasks/{task_id}/submit_code")
def submit_code(session_id: str, task_id: str, payload: schemas.CodeSubmission, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    task = _get_task_by_id(session.scenario, task_id)
    if not task or task.get("type") != "coding":
        raise HTTPException(status_code=400, detail="Task is not a coding task")
    result = sandbox.run_code(payload.language, payload.code, payload.tests_id)
    system_msg = models.Message(
        session_id=session_id,
        sender="system",
        text=f"Code execution result for {task_id}: {result}",
        task_id=task_id,
    )
    db.add(system_msg)
    db.commit()
    return {"task_id": task_id, "result": result}

@router.post("/{session_id}/tasks/{task_id}/submit_sql")
def submit_sql(session_id: str, task_id: str, payload: schemas.SqlSubmission, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    task = _get_task_by_id(session.scenario, task_id)
    if not task or task.get("type") != "sql":
        raise HTTPException(status_code=400, detail="Task is not a SQL task")
    result = sandbox.run_sql(payload.sql_scenario_id, payload.query)
    system_msg = models.Message(
        session_id=session_id,
        sender="system",
        text=f"SQL execution result for {task_id}: {result}",
        task_id=task_id,
    )
    db.add(system_msg)
    db.commit()
    return {"task_id": task_id, "result": result}

@router.post("/{session_id}/practice/sql")
def practice_sql(session_id: str, payload: PracticeSqlRequest, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    task = _get_task_by_id(session.scenario, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found in scenario")

    # Инструкция модели: она должна вызвать run_sql
    instruction = (
        f"Проверь решение кандидата для sql-задачи {payload.task_id} ({task.get('title','')}).\n"
        f"СНАЧАЛА вызови инструмент run_sql с sql_scenario_id='{payload.sql_scenario_id}' и query.\n"
        f"ПОТОМ объясни результат (ошибки/замечания), дай рекомендации и при необходимости оцени через score_task.\n\n"
        f"SQL:\n{payload.query}"
    )

    return _practice_agent_review(session=session, db=db, instruction=instruction, task_id=payload.task_id)

@router.post("/{session_id}/complete")
def complete_session(session_id: str, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.state = "completed"
    session.finished_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}

@router.post("/{session_id}/web-search")
def run_web_search(session_id: str, payload: schemas.WebSearchRequest, db: Session = Depends(get_db)):
    if not db.get(models.Session, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    results = web_search.web_search(payload.query, payload.top_k)
    return {"results": results}

@router.post("/{session_id}/practice/code")
def practice_code(session_id: str, payload: PracticeCodeRequest, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    task = _get_task_by_id(session.scenario, payload.task_id)
    if not task:
        raise HTTPException(status_code=400, detail=f"Task {payload.task_id} not found in scenario")
    session.current_task_id = payload.task_id
    db.commit()

    # Агентная проверка: модель сама вызывает tools по протоколу
    instruction = (
        f"Ты проверяешь coding-задачу {payload.task_id} ({task.get('title','')}).\n"
        "Кандидат написал код сам.\n"
        "Выполни проверку строго через инструменты:\n"
        "1) run_code(language, code=<candidate_code>)\n"
        "2) По результату выполнения (stdout/stderr/exit_code) сделай вывод и вызови score_task.\n\n"
        "ВАЖНО:\n"
        "- Нельзя писать 'score_task -> {...}' текстом. Используй только tool-вызов.\n"
        "- До вызова score_task не давай кандидату итоговую оценку.\n\n"
        f"КОД КАНДИДАТА:\n{payload.code}\n"
    )

    review = _practice_agent_review(
        session=session,
        db=db,
        instruction=instruction,
        task_id=payload.task_id,
    )

    return {"reply": review["reply"], "tool_results": review.get("tool_results", [])}


