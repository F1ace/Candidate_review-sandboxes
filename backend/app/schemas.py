from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ----- Role -----


class RoleBase(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None


class RoleOut(RoleBase):
    id: int

    class Config:
        from_attributes = True


# ----- Scenario -----


class ScenarioBase(BaseModel):
    role_id: int
    name: str
    slug: str
    description: Optional[str] = None
    difficulty: Optional[str] = None
    tasks: Optional[list[dict[str, Any]]] = None
    rag_corpus_id: Optional[int] = None
    sql_scenario_id: Optional[int] = None
    config: Optional[dict[str, Any]] = None


class ScenarioCreate(ScenarioBase):
    pass


class ScenarioUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    difficulty: Optional[str] = None
    tasks: Optional[list[dict[str, Any]]] = None
    rag_corpus_id: Optional[int] = None
    sql_scenario_id: Optional[int] = None
    config: Optional[dict[str, Any]] = None


class ScenarioOut(ScenarioBase):
    id: int

    class Config:
        from_attributes = True


# ----- Rag corpus / documents -----


class RagCorpusBase(BaseModel):
    name: str
    description: Optional[str] = None


class RagCorpusCreate(RagCorpusBase):
    pass


class RagCorpusOut(RagCorpusBase):
    id: int

    class Config:
        from_attributes = True


class DocumentCreate(BaseModel):
    filename: str = Field(description="Original file name")
    content: str = Field(description="Plain text content for indexing")
    metadata: Optional[dict[str, Any]] = None


class DocumentOut(BaseModel):
    id: int
    rag_corpus_id: int
    filename: str
    content: str
    content_type: Optional[str] = None
    storage_bucket: Optional[str] = None
    object_key: Optional[str] = None
    size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
    status: str
    created_at: datetime
    ingested_at: Optional[datetime] = None
    meta: Optional[dict[str, Any]] = Field(default=None, serialization_alias="metadata")

    class Config:
        from_attributes = True
        populate_by_name = True


# ----- SQL Scenario -----


class SqlScenarioBase(BaseModel):
    name: str
    description: Optional[str] = None
    db_schema: Optional[str] = None
    reference_solutions: Optional[dict[str, Any]] = None


class SqlScenarioCreate(SqlScenarioBase):
    pass


class SqlScenarioOut(SqlScenarioBase):
    id: int

    class Config:
        from_attributes = True


# ----- Session & chat -----


class SessionCreate(BaseModel):
    scenario_id: int
    role_id: int
    candidate_id: Optional[str] = None


class SessionOut(BaseModel):
    id: str
    scenario_id: int
    role_id: int
    candidate_id: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]
    state: str
    current_task_id: Optional[str]
    scores: Optional[dict[str, Any]]

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    text: str
    sender: str = Field(description="candidate | model | system | tool")
    task_id: Optional[str] = None


class MessageOut(BaseModel):
    id: int
    session_id: str
    sender: str
    text: str
    created_at: datetime
    task_id: Optional[str]

    class Config:
        from_attributes = True


# ----- Scoring -----


class ScoreCreate(BaseModel):
    task_id: str
    points: float
    comment: Optional[str] = None
    comments: Optional[list[str]] = None
    is_final: Optional[bool] = None
    question_index: Optional[int] = None


class ScoreOut(ScoreCreate):
    is_final: bool
    id: int
    session_id: str
    created_at: datetime

    class Config:
        from_attributes = True


class InterviewReportSectionOut(BaseModel):
    title: str
    summary: str
    highlights: list[str] = Field(default_factory=list)


class InterviewReportTaskOut(BaseModel):
    task_id: str
    title: str
    task_type: str
    score: Optional[float] = None
    max_points: float
    ratio: Optional[float] = None
    summary: str
    highlights: list[str] = Field(default_factory=list)
    score_comment: Optional[str] = None


class InterviewReportOut(BaseModel):
    session_id: str
    generated_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_minutes: int
    candidate_id: Optional[str] = None
    role_name: str
    role_slug: str
    scenario_name: str
    scenario_slug: str
    difficulty: Optional[str] = None
    headline: str
    executive_summary: str
    overall_assessment: str
    closing_note: str
    recommendation_label: str
    recommendation_summary: str
    generation_mode: str
    overall_score: float
    overall_max: float
    overall_ratio: Optional[float] = None
    scored_tasks: int
    total_tasks: int
    candidate_message_count: int
    model_message_count: int
    strengths: list[str] = Field(default_factory=list)
    growth_areas: list[str] = Field(default_factory=list)
    sections: list[InterviewReportSectionOut] = Field(default_factory=list)
    task_breakdown: list[InterviewReportTaskOut] = Field(default_factory=list)


# ----- Tools -----


class RagSearchRequest(BaseModel):
    query: str
    corpus_id: int
    top_k: int = 3


class RagSearchResult(BaseModel):
    document_id: int
    chunk_id: int
    filename: str
    snippet: str
    score: float
    metadata: Optional[dict[str, Any]] = None


class CodeSubmission(BaseModel):
    code: str
    language: str

class SqlSubmission(BaseModel):
    query: str
    sql_scenario_id: str
