from __future__ import annotations

from math import sqrt
from typing import Any, Iterable, List

from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..schemas import RagSearchResult
from .rag_embeddings import embedding_service


def _build_snippet(text_value: str, query: str, limit: int = 320) -> str:
    content = (text_value or "").strip()
    if not content:
        return ""
    if len(content) <= limit:
        return content

    lowered = content.lower()
    query_terms = [term.strip().lower() for term in query.split() if len(term.strip()) > 2]
    hit_pos = min((lowered.find(term) for term in query_terms if lowered.find(term) != -1), default=-1)
    if hit_pos == -1:
        return content[:limit].strip()

    start = max(0, hit_pos - limit // 3)
    end = min(len(content), start + limit)
    snippet = content[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet


def _vector_literal(vector: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(item):.12g}" for item in vector) + "]"


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sqrt(sum(a * a for a in left))
    right_norm = sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _serialize_hit(
    *,
    document_id: int,
    chunk_id: int,
    filename: str,
    content: str,
    query: str,
    score: float,
    metadata: dict[str, Any],
) -> RagSearchResult:
    return RagSearchResult(
        document_id=document_id,
        chunk_id=chunk_id,
        filename=filename,
        snippet=_build_snippet(content, query),
        score=score,
        metadata={
            "chunk_index": metadata.get("chunk_index"),
            "char_start": metadata.get("char_start"),
            "char_end": metadata.get("char_end"),
            "document_status": metadata.get("document_status"),
            "retrieval_backend": embedding_service.backend_name,
            "embedding_model": embedding_service.model_name,
        },
    )


def _serialize_chunk_hit(chunk: models.DocumentChunk, query: str, score: float) -> RagSearchResult:
    document = chunk.document
    return _serialize_hit(
        document_id=chunk.document_id,
        chunk_id=chunk.id,
        filename=document.filename if document else "document",
        content=chunk.content,
        query=query,
        score=score,
        metadata={
            "chunk_index": chunk.chunk_index,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "document_status": document.status if document else None,
        },
    )


def _ready_chunks_query(db: Session, rag_corpus_id: int):
    return (
        db.query(models.DocumentChunk)
        .join(models.Document, models.Document.id == models.DocumentChunk.document_id)
        .options(joinedload(models.DocumentChunk.document))
        .filter(
            models.Document.rag_corpus_id == rag_corpus_id,
            models.Document.status == "ready",
        )
    )


def _ensure_embeddings(db: Session, rag_corpus_id: int) -> None:
    chunks = _ready_chunks_query(db, rag_corpus_id).filter(models.DocumentChunk.embedding.is_(None)).all()
    if not chunks:
        return

    embeddings = embedding_service.embed_documents([chunk.content for chunk in chunks])
    if len(embeddings) != len(chunks):
        raise RuntimeError("Embedding backend returned an unexpected number of vectors")

    for chunk, embedding in zip(chunks, embeddings):
        chunk.embedding = embedding
        metadata = dict(chunk.meta or {})
        metadata.update(
            {
                "retrieval_backend": embedding_service.backend_name,
                "embedding_model": embedding_service.model_name,
            }
        )
        chunk.meta = metadata
    db.commit()


def _search_document_chunks_pgvector(
    *,
    db: Session,
    rag_corpus_id: int,
    query: str,
    query_embedding: list[float],
    top_k: int,
) -> List[RagSearchResult]:
    rows = (
        db.execute(
            text(
                """
                SELECT
                    dc.id AS chunk_id,
                    dc.document_id AS document_id,
                    dc.chunk_index AS chunk_index,
                    dc.char_start AS char_start,
                    dc.char_end AS char_end,
                    dc.content AS content,
                    d.filename AS filename,
                    d.status AS document_status,
                    1 - (dc.embedding <=> CAST(:query_embedding AS vector)) AS score
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE d.rag_corpus_id = :rag_corpus_id
                    AND d.status = 'ready'
                    AND dc.embedding IS NOT NULL
                ORDER BY dc.embedding <=> CAST(:query_embedding AS vector)
                LIMIT :top_k
                """
            ),
            {
                "rag_corpus_id": rag_corpus_id,
                "query_embedding": _vector_literal(query_embedding),
                "top_k": top_k,
            },
        )
        .mappings()
        .all()
    )

    return [
        _serialize_hit(
            document_id=int(row["document_id"]),
            chunk_id=int(row["chunk_id"]),
            filename=str(row["filename"] or "document"),
            content=str(row["content"] or ""),
            query=query,
            score=float(row["score"] or 0.0),
            metadata={
                "chunk_index": row["chunk_index"],
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "document_status": row["document_status"],
            },
        )
        for row in rows
    ]


def _search_document_chunks_python(
    *,
    db: Session,
    rag_corpus_id: int,
    query: str,
    query_embedding: list[float],
    top_k: int,
) -> List[RagSearchResult]:
    chunks = _ready_chunks_query(db, rag_corpus_id).filter(models.DocumentChunk.embedding.is_not(None)).all()
    hits = [
        (chunk, _cosine_similarity(query_embedding, chunk.embedding or []))
        for chunk in chunks
    ]
    hits.sort(key=lambda item: item[1], reverse=True)
    return [_serialize_chunk_hit(chunk, query, score) for chunk, score in hits[:top_k]]


def search_document_chunks(
    db: Session,
    rag_corpus_id: int,
    query: str,
    top_k: int = 3,
) -> List[RagSearchResult]:
    normalized_query = (query or "").strip()
    if not normalized_query:
        return []
    result_limit = max(1, int(top_k or 3))

    _ensure_embeddings(db, rag_corpus_id)
    query_embedding = embedding_service.embed_query(normalized_query)
    if db.get_bind().dialect.name == "postgresql":
        return _search_document_chunks_pgvector(
            db=db,
            rag_corpus_id=rag_corpus_id,
            query=normalized_query,
            query_embedding=query_embedding,
            top_k=result_limit,
        )

    return _search_document_chunks_python(
        db=db,
        rag_corpus_id=rag_corpus_id,
        query=normalized_query,
        query_embedding=query_embedding,
        top_k=result_limit,
    )


def search_documents(docs: Iterable[dict], query: str, top_k: int = 3) -> List[RagSearchResult]:
    normalized_query = (query or "").strip()
    if not normalized_query:
        return []
    result_limit = max(1, int(top_k or 3))

    prepared_docs = list(docs)
    if not prepared_docs:
        return []

    texts = [str(doc.get("content") or "") for doc in prepared_docs]
    document_embeddings = embedding_service.embed_documents(texts)
    query_embedding = embedding_service.embed_query(normalized_query)

    hits = []
    for doc, content, embedding in zip(prepared_docs, texts, document_embeddings):
        metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        hits.append(
            (
                doc,
                content,
                _cosine_similarity(query_embedding, embedding),
                metadata,
            )
        )

    hits.sort(key=lambda item: item[2], reverse=True)
    return [
        _serialize_hit(
            document_id=int(doc.get("id") or 0),
            chunk_id=int(doc.get("chunk_id") or doc.get("id") or 0),
            filename=str(doc.get("filename") or "document"),
            content=content,
            query=normalized_query,
            score=score,
            metadata={
                "chunk_index": metadata.get("chunk_index"),
                "char_start": metadata.get("char_start"),
                "char_end": metadata.get("char_end"),
                "document_status": metadata.get("document_status"),
            },
        )
        for doc, content, score, metadata in hits[:result_limit]
    ]
