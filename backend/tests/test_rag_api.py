from __future__ import annotations

import pytest

from app import models
from app.services import rag as rag_service


def test_rag_upload_and_search_indexes_chunks(client, db_session, embeddings_backend):
    corpus_resp = client.post("/rag/corpora", json={"name": "Docs", "description": "RAG corpus"})
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    upload_resp = client.post(
        f"/rag/corpora/{corpus_id}/documents/upload",
        files={
            "file": (
                "http-basics.txt",
                (
                    "Идемпотентность означает, что повторный вызов с теми же параметрами "
                    "не меняет результат сверх первого применения. POST по умолчанию не является идемпотентным."
                ).encode("utf-8"),
                "text/plain",
            )
        },
    )
    assert upload_resp.status_code == 201
    payload = upload_resp.json()
    assert payload["status"] == "ready"
    assert payload["storage_bucket"]
    assert payload["object_key"]

    document = db_session.get(models.Document, payload["id"])
    assert document is not None
    assert len(document.chunks) >= 1

    search_resp = client.post(
        "/rag/search",
        json={
            "corpus_id": corpus_id,
            "query": "POST идемпотентность",
            "top_k": 3,
        },
    )
    assert search_resp.status_code == 200
    results = search_resp.json()
    assert len(results) >= 1
    assert results[0]["chunk_id"] > 0
    assert "идемпотент" in results[0]["snippet"].lower()
    assert embeddings_backend.document_calls
    assert embeddings_backend.query_calls
    assert results[0]["metadata"]["retrieval_backend"] == "langchain_inmemory_vectorstore"
    assert results[0]["metadata"]["embedding_model"] == "fake-lmstudio-embedding"


def test_rag_search_does_not_fallback_when_embeddings_fail(client, db_session, monkeypatch):
    corpus_resp = client.post("/rag/corpora", json={"name": "Docs", "description": "RAG corpus"})
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    upload_resp = client.post(
        f"/rag/corpora/{corpus_id}/documents/upload",
        files={
            "file": (
                "http-basics.txt",
                (
                    "Идемпотентность означает, что повторный вызов с теми же параметрами "
                    "не меняет результат сверх первого применения. POST по умолчанию не является идемпотентным."
                ).encode("utf-8"),
                "text/plain",
            )
        },
    )
    assert upload_resp.status_code == 201

    def fail_embed_documents(texts):
        raise RuntimeError("embeddings backend offline")

    monkeypatch.setattr(rag_service.embedding_service, "embed_documents", fail_embed_documents)

    with pytest.raises(RuntimeError, match="embeddings backend offline"):
        rag_service.search_document_chunks(
            db=db_session,
            rag_corpus_id=corpus_id,
            query="POST идемпотентность",
            top_k=3,
        )
