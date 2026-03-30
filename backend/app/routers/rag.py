import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services.rag import search_document_chunks
from ..services.rag_ingestion import ingest_document_bytes

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/corpora", response_model=schemas.RagCorpusOut, status_code=status.HTTP_201_CREATED)
@router.post("/corpora/", response_model=schemas.RagCorpusOut, status_code=status.HTTP_201_CREATED)
def create_corpus(payload: schemas.RagCorpusCreate, db: Session = Depends(get_db)):
    corpus = models.RagCorpus(**payload.model_dump())
    db.add(corpus)
    db.commit()
    db.refresh(corpus)
    return corpus


@router.get("/corpora", response_model=list[schemas.RagCorpusOut])
@router.get("/corpora/", response_model=list[schemas.RagCorpusOut])
def list_corpora(db: Session = Depends(get_db)):
    return db.query(models.RagCorpus).all()


@router.get("/corpora/{corpus_id}", response_model=schemas.RagCorpusOut)
def get_corpus(corpus_id: int, db: Session = Depends(get_db)):
    corpus = db.get(models.RagCorpus, corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return corpus


@router.post(
    "/corpora/{corpus_id}/documents",
    response_model=schemas.DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
def add_document(corpus_id: int, payload: schemas.DocumentCreate, db: Session = Depends(get_db)):
    corpus = db.get(models.RagCorpus, corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    try:
        return ingest_document_bytes(
            db=db,
            corpus_id=corpus_id,
            filename=payload.filename,
            payload=payload.content.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            metadata=payload.metadata,
            extracted_text=payload.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/corpora/{corpus_id}/documents/upload",
    response_model=schemas.DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    corpus_id: int,
    file: UploadFile = File(...),
    metadata_json: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    corpus = db.get(models.RagCorpus, corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")

    try:
        metadata = json.loads(metadata_json) if metadata_json else None
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata_json must decode to an object")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        return ingest_document_bytes(
            db=db,
            corpus_id=corpus_id,
            filename=file.filename or "document.txt",
            payload=payload,
            content_type=file.content_type or "application/octet-stream",
            metadata=metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/corpora/{corpus_id}/documents", response_model=list[schemas.DocumentOut])
def list_documents(corpus_id: int, db: Session = Depends(get_db)):
    corpus = db.get(models.RagCorpus, corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return db.query(models.Document).filter_by(rag_corpus_id=corpus_id).all()


@router.post("/search", response_model=list[schemas.RagSearchResult])
def rag_search(payload: schemas.RagSearchRequest, db: Session = Depends(get_db)):
    corpus = db.get(models.RagCorpus, payload.corpus_id)
    if not corpus:
        raise HTTPException(status_code=404, detail="Corpus not found")
    return search_document_chunks(
        db=db,
        rag_corpus_id=payload.corpus_id,
        query=payload.query,
        top_k=payload.top_k,
    )
