import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)

    scenarios: Mapped[list["Scenario"]] = relationship("Scenario", back_populates="role")


class RagCorpus(Base):
    __tablename__ = "rag_corpora"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    documents: Mapped[list["Document"]] = relationship("Document", back_populates="corpus", cascade="all, delete-orphan")
    scenarios: Mapped[list["Scenario"]] = relationship("Scenario", back_populates="rag_corpus")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rag_corpus_id: Mapped[int] = mapped_column(ForeignKey("rag_corpora.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(String(255))
    storage_bucket: Mapped[Optional[str]] = mapped_column(String(255))
    object_key: Mapped[Optional[str]] = mapped_column(String(512))
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, default={})

    corpus: Mapped["RagCorpus"] = relationship("RagCorpus", back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSON, default={})
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")


class SqlScenario(Base):
    __tablename__ = "sql_scenarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    db_schema: Mapped[Optional[str]] = mapped_column(Text)
    reference_solutions: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    scenarios: Mapped[list["Scenario"]] = relationship("Scenario", back_populates="sql_scenario")


class Scenario(Base):
    __tablename__ = "scenarios"
    __table_args__ = (UniqueConstraint("role_id", "slug", name="uq_scenario_role_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    difficulty: Mapped[Optional[str]] = mapped_column(String(50))
    tasks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    rag_corpus_id: Mapped[Optional[int]] = mapped_column(ForeignKey("rag_corpora.id"))
    sql_scenario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sql_scenarios.id"))
    config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    role: Mapped["Role"] = relationship("Role", back_populates="scenarios")
    rag_corpus: Mapped[Optional["RagCorpus"]] = relationship("RagCorpus", back_populates="scenarios")
    sql_scenario: Mapped[Optional["SqlScenario"]] = relationship("SqlScenario", back_populates="scenarios")
    sessions: Mapped[list["Session"]] = relationship("Session", back_populates="scenario")
    task_items: Mapped[list["Task"]] = relationship(
    "Task",
    back_populates="scenario",
    cascade="all, delete-orphan"
)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    candidate_id: Mapped[Optional[str]] = mapped_column(String(128))
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    state: Mapped[str] = mapped_column(String(50), default="active")
    current_task_id: Mapped[Optional[str]] = mapped_column(String(128))
    scores: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    raw_log: Mapped[Optional[str]] = mapped_column(Text)

    scenario: Mapped["Scenario"] = relationship("Scenario", back_populates="sessions")
    role: Mapped["Role"] = relationship("Role")
    messages: Mapped[list["Message"]] = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    score_entries: Mapped[list["Score"]] = relationship("Score", back_populates="session", cascade="all, delete-orphan")
    fact_validations: Mapped[list["TheoryFactValidation"]] = relationship(
        "TheoryFactValidation",
        back_populates="session",
        cascade="all, delete-orphan",
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    sender: Mapped[str] = mapped_column(String(50), nullable=False)  # candidate | model | system | tool
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    task_id: Mapped[Optional[str]] = mapped_column(String(128))

    session: Mapped["Session"] = relationship("Session", back_populates="messages")
    fact_validations: Mapped[list["TheoryFactValidation"]] = relationship(
        "TheoryFactValidation",
        back_populates="candidate_message",
    )


class TheoryFactValidation(Base):
    __tablename__ = "theory_fact_validations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    question_index: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evidence: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    session: Mapped["Session"] = relationship("Session", back_populates="fact_validations")
    candidate_message: Mapped["Message"] = relationship("Message", back_populates="fact_validations")


class Score(Base):
    __tablename__ = "scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    points: Mapped[float] = mapped_column(Float, nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    question_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship("Session", back_populates="score_entries")

class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("scenario_id", "external_id", name="uq_task_scenario_external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False, index=True)

    external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)   # theory | coding | sql
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description_for_candidate: Mapped[Optional[str]] = mapped_column(Text)
    max_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    language: Mapped[Optional[str]] = mapped_column(String(50))
    sql_scenario_ref: Mapped[Optional[str]] = mapped_column(String(128))
    starter_code: Mapped[Optional[str]] = mapped_column(Text)
    statement_md: Mapped[Optional[str]] = mapped_column(Text)

    related_topics: Mapped[Optional[list[str]]] = mapped_column(JSON)
    questions: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON)
    extra_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    scenario: Mapped["Scenario"] = relationship("Scenario", back_populates="task_items")

    test_cases: Mapped[list["TestCase"]] = relationship(
        "TestCase",
        secondary="task_test_cases",
        back_populates="tasks",
    )

class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    language: Mapped[str] = mapped_column(String(50), default="python", nullable=False)
    input_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    expected_output: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    checker_source: Mapped[Optional[str]] = mapped_column(Text)

    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    tasks: Mapped[list["Task"]] = relationship(
        "Task",
        secondary="task_test_cases",
        back_populates="test_cases",
    )

class TaskTestCase(Base):
    __tablename__ = "task_test_cases"
    __table_args__ = (
        UniqueConstraint("task_id", "test_case_id", name="uq_task_test_case"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    test_case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)

    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
