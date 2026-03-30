from __future__ import annotations

from app import models


def test_rag_upload_and_search_indexes_chunks(client, db_session):
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
