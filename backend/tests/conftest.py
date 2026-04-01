from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


TESTS_DIR = Path(__file__).resolve().parent
TEST_DB_PATH = TESTS_DIR / "test_app.sqlite3"

os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB_PATH.as_posix()}")
os.environ.setdefault("LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
os.environ.setdefault("MINIO_ENDPOINT", "localhost:9000")
os.environ.setdefault("MINIO_ACCESS_KEY", "minioadmin")
os.environ.setdefault("MINIO_SECRET_KEY", "minioadmin")
os.environ.setdefault("MINIO_BUCKET", "rag-documents-tests")
os.environ.setdefault("MINIO_SECURE", "false")

from app import main as main_module, models  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services import object_storage, rag as rag_service, rag_embeddings, rag_ingestion  # noqa: E402


class FakeStorage:
    def __init__(self) -> None:
        self.bucket = "fake-bucket"
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, object_key: str, payload: bytes, content_type: str) -> dict[str, str | int]:
        self.objects[object_key] = payload
        return {
            "bucket": self.bucket,
            "object_key": object_key,
            "size_bytes": len(payload),
            "content_type": content_type,
        }


class FakeEmbeddingService:
    def __init__(self) -> None:
        self.model_name = "fake-lmstudio-embedding"
        self.backend_name = "langchain_inmemory_vectorstore"
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * 32
        tokens = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = digest[0] % len(vector)
            sign = 1.0 if digest[1] % 2 == 0 else -1.0
            weight = 1.0 + (digest[2] / 255.0)
            vector[index] += sign * weight

        norm = sum(value * value for value in vector) ** 0.5
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        batch = [str(item) for item in texts]
        self.document_calls.append(batch)
        return [self._embed(text) for text in batch]

    def embed_query(self, text: str) -> list[float]:
        query = str(text)
        self.query_calls.append(query)
        return self._embed(query)


@pytest.fixture(autouse=True)
def reset_database():
    engine.dispose()
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()


@pytest.fixture(autouse=True)
def storage_backend(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if request.node.get_closest_marker("real_storage"):
        yield None
        return

    fake_storage = FakeStorage()
    monkeypatch.setattr(rag_ingestion, "storage_service", fake_storage)
    monkeypatch.setattr(object_storage, "storage_service", fake_storage)
    yield fake_storage


@pytest.fixture(autouse=True)
def embeddings_backend(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if request.node.get_closest_marker("real_embeddings"):
        yield None
        return

    fake_embeddings = FakeEmbeddingService()
    monkeypatch.setattr(rag_embeddings, "embedding_service", fake_embeddings)
    monkeypatch.setattr(rag_service, "embedding_service", fake_embeddings)
    yield fake_embeddings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module, "seed_defaults", lambda: None)
    with TestClient(main_module.app) as test_client:
        yield test_client


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def theory_scenario_factory(db_session):
    def factory(*, rag_corpus_id: int | None = None) -> models.Scenario:
        role = models.Role(name="Backend", slug="backend-test", description="test role")
        db_session.add(role)
        db_session.flush()

        scenario = models.Scenario(
            role_id=role.id,
            name="Theory with docs",
            slug="theory-with-docs",
            description="theory rag scenario",
            difficulty="middle",
            rag_corpus_id=rag_corpus_id,
            tasks=[
                {
                    "id": "T-DOCS",
                    "type": "theory",
                    "title": "Theory docs",
                    "max_points": 10,
                    "questions": [
                        "Что такое идемпотентность и как она связана с POST?",
                    ],
                }
            ],
            config={},
        )
        db_session.add(scenario)
        db_session.commit()
        db_session.refresh(scenario)
        return scenario

    return factory
