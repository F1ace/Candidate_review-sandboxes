from __future__ import annotations

import pytest
from minio import Minio

from app.services.object_storage import ObjectStorage
from app.services import object_storage, rag_ingestion
from app.config import settings


@pytest.mark.e2e
@pytest.mark.real_storage
def test_document_upload_persists_object_in_minio(client, monkeypatch):
    storage = ObjectStorage()

    try:
        storage.ensure_bucket()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MinIO is not available for integration test: {exc}")

    monkeypatch.setattr(rag_ingestion, "storage_service", storage)
    monkeypatch.setattr(object_storage, "storage_service", storage)

    corpus_resp = client.post("/rag/corpora", json={"name": "MinIO corpus", "description": "real storage"})
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    upload_resp = client.post(
        f"/rag/corpora/{corpus_id}/documents/upload",
        files={
            "file": (
                "minio-check.txt",
                b"PUT is idempotent. POST is usually not idempotent.",
                "text/plain",
            )
        },
    )
    assert upload_resp.status_code == 201
    payload = upload_resp.json()

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=bool(settings.minio_secure),
    )
    stat = client.stat_object(payload["storage_bucket"], payload["object_key"])
    assert stat.size == payload["size_bytes"]
