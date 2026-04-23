from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ... import models, schemas
from ...database import get_db
from ...services.session_reporting import generate_session_report
from .router import router


@router.post("/{session_id}/report", response_model=schemas.InterviewReportOut)
def build_report(session_id: str, db: Session = Depends(get_db)):
    session = db.get(models.Session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return generate_session_report(session, db)
