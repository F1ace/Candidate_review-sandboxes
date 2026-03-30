from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, List

from sqlalchemy.orm import Session, joinedload

from .. import models
from ..schemas import RagSearchResult


def _tokenize(text: str) -> Counter[str]:
    tokens = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
    return Counter(tokens)


def _cosine_similarity(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = set(a) & set(b)
    num = sum(a[t] * b[t] for t in intersection)
    denom = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / denom if denom else 0.0


def _build_snippet(text: str, query: str, limit: int = 320) -> str:
    content = (text or "").strip()
    if not content:
        return ""
    if len(content) <= limit:
        return content

    lowered = content.lower()
    query_terms = [term for term in _tokenize(query) if len(term) > 2]
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


def _serialize_chunk_result(chunk: models.DocumentChunk, query: str, score: float) -> RagSearchResult:
    return RagSearchResult(
        document_id=chunk.document_id,
        chunk_id=chunk.id,
        filename=chunk.document.filename,
        snippet=_build_snippet(chunk.content, query),
        score=score,
        metadata={
            "chunk_index": chunk.chunk_index,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "document_status": chunk.document.status,
        },
    )


def search_document_chunks(
    db: Session,
    rag_corpus_id: int,
    query: str,
    top_k: int = 3,
) -> List[RagSearchResult]:
    query_tokens = _tokenize(query)
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

    scored: list[tuple[models.DocumentChunk, float]] = []
    for chunk in chunks:
        chunk_tokens = _tokenize(chunk.content)
        score = _cosine_similarity(query_tokens, chunk_tokens)
        if score <= 0:
            continue
        scored.append((chunk, score))

    scored.sort(key=lambda item: (item[1], -item[0].chunk_index), reverse=True)
    return [_serialize_chunk_result(chunk, query, score) for chunk, score in scored[:top_k]]


def search_documents(docs: Iterable[dict], query: str, top_k: int = 3) -> List[RagSearchResult]:
    query_tokens = _tokenize(query)
    scored: list[tuple[dict, float]] = []
    for doc in docs:
        score = _cosine_similarity(query_tokens, _tokenize(doc.get("content") or ""))
        if score <= 0:
            continue
        scored.append((doc, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    results: list[RagSearchResult] = []
    for doc, score in scored[:top_k]:
        snippet_source = str(doc.get("content") or "")
        results.append(
            RagSearchResult(
                document_id=int(doc["id"]),
                chunk_id=int(doc.get("chunk_id") or doc["id"]),
                filename=str(doc.get("filename") or "document"),
                snippet=_build_snippet(snippet_source, query),
                score=score,
                metadata=doc.get("metadata") if isinstance(doc.get("metadata"), dict) else None,
            )
        )
    return results
