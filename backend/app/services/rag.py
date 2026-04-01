from __future__ import annotations

from typing import Iterable, List

from langchain_core.documents import Document as LangChainDocument
from langchain_core.vectorstores import InMemoryVectorStore
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..schemas import RagSearchResult
from .rag_embeddings import embedding_service


def _build_snippet(text: str, query: str, limit: int = 320) -> str:
    content = (text or "").strip()
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


def _build_vector_store(documents: list[LangChainDocument]) -> InMemoryVectorStore:
    vector_store = InMemoryVectorStore(embedding=embedding_service)
    vector_store.add_documents(documents, ids=[doc.id or "" for doc in documents])
    return vector_store


def _chunk_to_langchain_document(chunk: models.DocumentChunk) -> LangChainDocument:
    return LangChainDocument(
        id=str(chunk.id),
        page_content=chunk.content,
        metadata={
            "document_id": chunk.document_id,
            "chunk_id": chunk.id,
            "filename": chunk.document.filename,
            "chunk_index": chunk.chunk_index,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "document_status": chunk.document.status,
            "retrieval_backend": embedding_service.backend_name,
            "embedding_model": embedding_service.model_name,
        },
    )


def _serialize_langchain_hit(doc: LangChainDocument, query: str, score: float) -> RagSearchResult:
    metadata = doc.metadata or {}
    return RagSearchResult(
        document_id=int(metadata.get("document_id") or 0),
        chunk_id=int(metadata.get("chunk_id") or 0),
        filename=str(metadata.get("filename") or "document"),
        snippet=_build_snippet(doc.page_content, query),
        score=score,
        metadata={
            "chunk_index": metadata.get("chunk_index"),
            "char_start": metadata.get("char_start"),
            "char_end": metadata.get("char_end"),
            "document_status": metadata.get("document_status"),
            "retrieval_backend": metadata.get("retrieval_backend"),
            "embedding_model": metadata.get("embedding_model"),
        },
    )


def search_document_chunks(
    db: Session,
    rag_corpus_id: int,
    query: str,
    top_k: int = 3,
) -> List[RagSearchResult]:
    chunks = (
        db.query(models.DocumentChunk)
        .join(models.Document, models.Document.id == models.DocumentChunk.document_id)
        .options(joinedload(models.DocumentChunk.document))
        .filter(
            models.Document.rag_corpus_id == rag_corpus_id,
            models.Document.status == "ready",
        )
        .all()
    )

    if not chunks:
        return []

    vector_store = _build_vector_store([_chunk_to_langchain_document(chunk) for chunk in chunks])
    hits = vector_store.similarity_search_with_score(query, k=top_k)
    return [_serialize_langchain_hit(doc, query, score) for doc, score in hits]


def search_documents(docs: Iterable[dict], query: str, top_k: int = 3) -> List[RagSearchResult]:
    langchain_docs: list[LangChainDocument] = []
    for doc in docs:
        langchain_docs.append(
            LangChainDocument(
                id=str(doc.get("chunk_id") or doc.get("id") or ""),
                page_content=str(doc.get("content") or ""),
                metadata={
                    "document_id": int(doc.get("id") or 0),
                    "chunk_id": int(doc.get("chunk_id") or doc.get("id") or 0),
                    "filename": str(doc.get("filename") or "document"),
                    "chunk_index": (doc.get("metadata") or {}).get("chunk_index") if isinstance(doc.get("metadata"), dict) else None,
                    "char_start": (doc.get("metadata") or {}).get("char_start") if isinstance(doc.get("metadata"), dict) else None,
                    "char_end": (doc.get("metadata") or {}).get("char_end") if isinstance(doc.get("metadata"), dict) else None,
                    "document_status": (doc.get("metadata") or {}).get("document_status") if isinstance(doc.get("metadata"), dict) else None,
                    "retrieval_backend": embedding_service.backend_name,
                    "embedding_model": embedding_service.model_name,
                },
            )
        )

    if not langchain_docs:
        return []

    vector_store = _build_vector_store(langchain_docs)
    hits = vector_store.similarity_search_with_score(query, k=top_k)
    return [_serialize_langchain_hit(doc, query, score) for doc, score in hits]
