from datetime import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from ... import models, schemas
from ...database import get_db
from ...services import sandbox, web_search, sql_runner, sql_evaluator
from ...services.theory_rag import (find_candidate_answer_message, get_existing_validation, theory_rag_required,)
from .practice import _practice_agent_review, _practice_sql_agent_review
from .router import router
from .schemas import PracticeCodeRequest, PracticeSqlRequest
from .state import _get_task_by_id
from .state import advance_task_if_needed
from .dispatch import (_aggregate_theory_intermediate_scores, _build_tests_payload, _compute_final_theory_points, _resolve_score_task_is_final, _theory_ready_for_scoring, _validate_theory_intermediate_score_args, _validate_final_theory_comment, _validate_final_theory_comments, _validate_theory_comment_not_template, normalize_sandbox_result,)

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
    return db.query(models.Message).filter_by(session_id=session_id).order_by(models.Message.created_at.asc(), models.Message.id.asc()).all()

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
    theory_max_points = int(task.get("max_points", 10) or 10)
    points = float(int(round(float(payload.points))))
    comment = (payload.comment or "").strip()
    final_comments = payload.comments or []
    request_payload = payload.model_dump(exclude_unset=True)
    is_final = _resolve_score_task_is_final(
        request_payload,
        task_type=task_type,
        question_index=payload.question_index,
    )

    if not comment:
        raise HTTPException(status_code=400, detail="comment is required and must be non-empty")

    if task_type == "theory":
        if points < 1 or points > theory_max_points:
            raise HTTPException(status_code=400, detail=f"Theory score should be within [1, {theory_max_points}]",)

        template_error = _validate_theory_comment_not_template(comment)
        if template_error:
            raise HTTPException(status_code=400, detail=template_error)

        if is_final:
            final_comment_error = _validate_final_theory_comment(comment)
            if final_comment_error:
                raise HTTPException(status_code=400, detail=final_comment_error)

            final_comments_error = _validate_final_theory_comments(task, final_comments)
            if final_comments_error:
                raise HTTPException(status_code=400, detail=final_comments_error)

        question_index = payload.question_index
        if not is_final:
            validation_error = _validate_theory_intermediate_score_args(task, question_index)
            if validation_error:
                raise HTTPException(status_code=400, detail=validation_error)
            question_index = int(question_index)
            if theory_rag_required(session, db):
                candidate_message = find_candidate_answer_message(session, db, task, question_index)
                if not candidate_message:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Candidate has not answered question_index={question_index} yet.",
                    )
                validation = get_existing_validation(
                    session_id=session.id,
                    task_id=payload.task_id,
                    question_index=question_index,
                    candidate_message_id=candidate_message.id,
                    db=db,
                )
                if not validation:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Theory answer must be validated against scenario documents before scoring. "
                            "Run rag_search first."
                        ),
                    )
        else:
            if not _theory_ready_for_scoring(session, db, task):
                raise HTTPException(status_code=400, detail="Theory block is not finished yet. Ask all questions first.")

            aggregated = _aggregate_theory_intermediate_scores(session, db, payload.task_id)

            if aggregated["expected_questions"] and aggregated["missing_questions"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Theory intermediate scores are incomplete. "
                        f"Missing question_index: {aggregated['missing_questions']}"
                    ),
                )

            if aggregated["avg_points"] is None:
                raise HTTPException(status_code=400, detail="Theory final score requires intermediate scores first.")

            points = _compute_final_theory_points(points, aggregated["avg_points"], theory_max_points,)
            question_index = None

        score = models.Score(
            session_id=session_id,
            task_id=payload.task_id,
            points=points,
            comment=comment,
            is_final=is_final,
            question_index=question_index,
            score_type="theory_final" if is_final else "theory_intermediate",
        )
        db.add(score)

        if is_final:
            current_scores = session.scores or {}
            session.scores = {**current_scores, payload.task_id: points}

        db.commit()
        db.refresh(score)
        if is_final:
            setattr(score, "comments", final_comments)
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

    task_row = (
        db.query(models.Task)
        .filter(
            models.Task.scenario_id == session.scenario_id,
            models.Task.external_id == task_id,
        )
        .first()
    )

    if not task_row or task_row.task_type != "coding":
        raise HTTPException(status_code=400, detail="Task is not a coding task")

    tests_payload = _build_tests_payload(task_row)
    if not tests_payload:
        raise HTTPException(status_code=400, detail=f"No active testcases linked to task {task_id}")

    raw_result = sandbox.run_code(payload.language, payload.code, tests_payload)
    result = normalize_sandbox_result(raw_result)

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

    task_row = (
        db.query(models.Task)
        .filter(
            models.Task.scenario_id == session.scenario_id,
            models.Task.external_id == task_id,
        )
        .first()
    )
    if not task_row:
        raise HTTPException(status_code=404, detail="Task not found")

    if task_row.task_type != "sql":
        raise HTTPException(status_code=400, detail="Task is not a SQL task")

    result = sql_runner.run_sql_for_task(
        db=db,
        task_row=task_row,
        query=payload.query,
    )

    system_msg = models.Message(
        session_id=session_id,
        sender="system",
        text=f"SQL execution result for {task_id}: {result}",
        task_id=task_id,
    )
    db.add(system_msg)
    db.commit()

    return {
        "task_id": task_id,
        "result": result,
    }

def _practice_sql_review(
    *,
    session: models.Session,
    db: Session,
    task_row: models.Task,
    query: str,
):
    instruction = (
        f"Ты проверяешь SQL-решение кандидата для задачи {task_row.external_id} ({task_row.title}).\n\n"
        f"Задача для кандидата:\n{task_row.description_for_candidate}\n\n"
        f"SQL кандидата:\n{query}\n\n"
        f"Важно:\n"
        f"1. Сначала ОБЯЗАТЕЛЬНО вызови run_sql с task_id='{task_row.external_id}' и query кандидата.\n"
        f"2. Затем проанализируй результат выполнения SQL.\n"
        f"3. После этого ОБЯЗАТЕЛЬНО вызови score_task для task_id='{task_row.external_id}'.\n"
        f"4. И comment в score_task, и финальный ответ кандидату должны содержать РОВНО 4 непустые секции:\n"
        f"Корректность: ...\n"
        f"Качество решения: ...\n"
        f"Работа с SQL: ...\n"
        f"Что можно улучшить: ...\n"
    )

    return _practice_sql_agent_review(
        session=session,
        db=db,
        instruction=instruction,
        task_id=task_row.external_id,
        candidate_query=query,
    )

@router.post("/{session_id}/practice/sql")
def practice_sql(session_id: str, payload: PracticeSqlRequest, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    task_row = (
        db.query(models.Task)
        .filter(
            models.Task.scenario_id == session.scenario_id,
            models.Task.external_id == payload.task_id,
        )
        .first()
    )
    if not task_row:
        raise HTTPException(status_code=404, detail="Task not found")

    if task_row.task_type != "sql":
        raise HTTPException(status_code=400, detail="Task is not a SQL task")

    session.current_task_id = payload.task_id
    db.commit()

    return _practice_sql_review(
        session=session,
        db=db,
        task_row=task_row,
        query=payload.query,
    )

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
        "Кандидат уже написал код.\n"
        "Твоя задача — строго завершить пайплайн в таком порядке:\n"
        "1) вызвать run_code\n"
        "2) дождаться результата sandbox\n"
        "3) вызвать score_task\n"
        "4) только после этого дать кандидату финальный комментарий.\n\n"
        "ВАЖНО:\n"
        "- Не переписывай и не пересобирай код кандидата.\n"
        "- Не оборачивай код в markdown fences.\n"
        "- Не передавай в run_code свой вариант кода.\n"
        "- Оценивай решение только после результата sandbox.\n"
        "- Балл и комментарий должны формироваться на основе passrate тестов, корректности решения, качества кода, читаемости, структуры, нейминга и обработки крайних случаев.\n"
        "- Обязательно учитывай, какие именно тесты упали и какие ошибки вернул sandbox.\n"
        "- Если тесты падают на одинаковом сценарии, укажи, какая часть логики, вероятно, реализована неверно или нестабильно.\n"
        "- Если это уместно, кратко оцени сложность и эффективность решения.\n"
        "- Уже в ПЕРВОМ вызове score_task заполни comment полностью.\n"
        "- points передай отдельно как число, а не текстом внутри comment.\n"
        "- В score_task.comment используй ровно 4 секции:\n"
        "  Корректность:\n"
        "  Качество кода:\n"
        "  Сложность и эффективность:\n"
        "  Что можно улучшить:\n"
        "- Все 4 секции обязательны и не должны быть пустыми.\n"
        "- В каждой секции должен быть обычный законченный текст из 1-3 предложений.\n"
        "- Нельзя использовать квадратные скобки, шаблонные инструкции или текст вида 'заполни'.\n"
        "- Не дублируй в comment балл и количество пройденных тестов: они отображаются отдельно.\n"
        "- Если все тесты пройдены, это не освобождает от необходимости заполнить разделы 'Качество кода', 'Сложность и эффективность' и 'Что можно улучшить'.\n"
        "- Нельзя оставлять только заголовок без содержимого.\n"
        "- Нельзя использовать квадратные скобки, шаблонные инструкции или текст вида 'заполни'.\n"
        "- Для секции 'Сложность и эффективность' можно написать, что для данной задачи отдельные замечания по сложности несущественны, если это действительно так.\n"
        "- Балл не должен определяться только по passrate: учитывай также качество решения.\n"
        "- Финальный ответ кандидату должен быть обычным текстом, без JSON и без служебных полей.\n\n"
        f"КОД КАНДИДАТА:\n{payload.code}\n"
    )

    review = _practice_agent_review(
        session=session,
        db=db,
        instruction=instruction,
        task_id=payload.task_id,
    )

    return {"reply": review["reply"], "tool_results": review.get("tool_results", [])}
