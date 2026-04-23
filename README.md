# Chat-Review Monorepo

Monorepo for interview sessions with theory, coding, and SQL tasks.

## Stack

- `backend`: FastAPI + SQLAlchemy + Alembic
- `frontend`: React + Vite + TypeScript
- `sandbox-code`: isolated Python code runner
- `sandbox-sql`: isolated SQL runner (uses `sqlite3` internally by design)
- Main backend database: PostgreSQL
- Object storage for RAG documents: MinIO

## Runtime Notes

- Backend runtime is configured for PostgreSQL.
- Alembic is used for schema evolution.
- `sandbox-sql` keeps SQLite-based execution intentionally; this is not backend DB storage.

## Quick Start (Docker Compose)

```bash
docker compose up --build
```

Services:

- Backend: `http://localhost:8000/health`
- Sandbox code: `http://localhost:8001/health`
- Sandbox SQL: `http://localhost:8002/health`
- Postgres: `localhost:5432` (`postgres/postgres`, DB `reviewer`)
- MinIO API: `http://localhost:9000`
- MinIO Console: `http://localhost:9001` (`minioadmin/minioadmin`)

## Local Backend Run

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

## Local Frontend Run

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

Build frontend for backend static serving:

```bash
npm run build
```

## Environment Variables

Use `.env.example` as base. Key runtime variables:

- `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/reviewer`
- `LM_STUDIO_URL=http://localhost:1234/v1/chat/completions`
- `LM_MODEL=openai/gpt-oss-20b`
- `SANDBOX_CODE_URL=http://localhost:8001/run_code`
- `SANDBOX_SQL_URL=http://localhost:8002/run_sql`
- `ALLOW_ORIGINS=*`

## Main API Groups

- `POST /sessions`, `GET /sessions/{id}`
- `GET/POST /sessions/{id}/messages`
- `POST /sessions/{id}/tasks/{task_id}/submit_code`
- `POST /sessions/{id}/tasks/{task_id}/submit_sql`
- `GET /sessions/{id}/lm/chat-stream`, `POST /sessions/{id}/lm/chat`
- `POST /sessions/{id}/practice/code`, `POST /sessions/{id}/practice/sql`
- CRUD: `/roles`, `/scenarios`, `/sql-scenarios`, `/rag/*`

## Manual RAG Verification

Below is a practical checklist for verifying that RAG works end-to-end, including theory validation against uploaded documents.

### 1. Start the required services

```powershell
docker compose up -d db minio

cd backend
$env:DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/reviewer"
$env:MINIO_ENDPOINT="localhost:9000"
$env:MINIO_ACCESS_KEY="minioadmin"
$env:MINIO_SECRET_KEY="minioadmin"
$env:MINIO_BUCKET="rag-documents"
$env:MINIO_SECURE="false"
$env:LM_STUDIO_URL="http://localhost:1234/v1/chat/completions"
$env:LM_MODEL="openai/gpt-oss-20b"
..\.venv-rag\Scripts\python -m alembic upgrade head
..\.venv-rag\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

Checks:

- Swagger/OpenAPI: `http://localhost:8000/docs`
- LM availability: `GET /lm/ping`
- MinIO Console: `http://localhost:9001`

If `GET /lm/ping` fails, you can still verify document ingestion and search, but not the interview loop.

### 2. Smoke-test RAG without interview flow

Use Swagger:

1. `POST /rag/corpora`

```json
{
  "name": "Manual RAG Check",
  "description": "manual test"
}
```

2. `POST /rag/corpora/{corpus_id}/documents/upload`

Upload a `.txt` file, for example:

```text
Идемпотентность означает, что повторный вызов операции с теми же данными
не меняет результат после первого применения. POST обычно не считается идемпотентным.
```

3. `POST /rag/search`

```json
{
  "corpus_id": 1,
  "query": "Что такое идемпотентность и почему POST не идемпотентен?",
  "top_k": 3
}
```

Expected result:

- the response contains `snippet`, `chunk_id`, and `score`
- the uploaded object appears in MinIO bucket `rag-documents`
- this confirms ingestion, chunking, storage, and retrieval

### 3. Verify theory RAG validation in an interview session

First, get a valid `role_id` via `GET /roles`.

Then create a scenario via `POST /scenarios`:

```json
{
  "role_id": 1,
  "name": "RAG Theory Manual",
  "slug": "rag-theory-manual",
  "description": "manual rag theory test",
  "difficulty": "middle",
  "rag_corpus_id": 1,
  "tasks": [
    {
      "id": "T-RAG",
      "type": "theory",
      "title": "HTTP theory",
      "max_points": 10,
      "questions": [
        "Что такое идемпотентность и почему POST обычно не считается идемпотентным?"
      ]
    }
  ],
  "config": {}
}
```

Create a session via `POST /sessions`:

```json
{
  "scenario_id": 1,
  "role_id": 1,
  "candidate_id": "manual-rag"
}
```

Then test either through the UI or the session chat endpoints.

Recommended answer from the candidate:

```text
Идемпотентность означает, что повтор операции с теми же входными данными не меняет итог после первого применения. POST обычно не считается идемпотентным.
```

Expected behavior:

- the theory answer is validated against scenario documents before scoring
- evidence is stored server-side
- intermediate theory score is saved only after validation

### 4. Inspect the database state directly

Connect to PostgreSQL:

```powershell
docker exec -it reviewer-postgres psql -U postgres -d reviewer
```

Run:

```sql
select id, filename, status, storage_bucket, object_key from documents order by id desc;
select document_id, count(*) from document_chunks group by document_id;
select session_id, task_id, question_index, result_count from theory_fact_validations order by id desc;
select session_id, task_id, question_index, points, is_final from scores order by id desc;
```

Expected result:

- a document exists in `documents`
- chunks exist in `document_chunks`
- a validation record exists in `theory_fact_validations`
- `result_count > 0`
- intermediate and final theory scores appear in `scores`

### 5. Run automated checks

Backend:

```powershell
cd backend
..\.venv-rag\Scripts\python -m pytest tests -q
```

Targeted RAG-related checks:

```powershell
cd backend
..\.venv-rag\Scripts\python -m pytest tests\test_rag_api.py tests\test_theory_rag_streaming_e2e.py tests\test_theory_rag_nonstream_e2e.py tests\test_minio_integration_e2e.py -q
```

Frontend build:

```powershell
cd frontend
npm run build
```
