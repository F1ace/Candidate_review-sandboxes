from fastapi import Depends
from sqlalchemy.orm import Session

from ...database import get_db
from .nonstream import call_model as call_model_impl
from .router import router
from .streaming import stream_model as stream_model_impl


@router.post("/{session_id}/lm/chat")
def call_model(session_id: str, db: Session = Depends(get_db)):
    return call_model_impl(session_id=session_id, db=db)


@router.get("/{session_id}/lm/chat-stream")
def stream_model(session_id: str):
    return stream_model_impl(session_id=session_id)
