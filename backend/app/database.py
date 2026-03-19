from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    """Base class for ORM models."""


def _engine_connect_args(database_url: str) -> dict[str, Any]:
    # Keep legacy SQLite compatibility for local dev while PostgreSQL is default runtime.
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


engine = create_engine(
    settings.database_url,
    connect_args=_engine_connect_args(settings.database_url),
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)


def get_db():
    """FastAPI dependency providing a transactional DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
