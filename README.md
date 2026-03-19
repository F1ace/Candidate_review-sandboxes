# Chat-Review Monorepo

Monorepo for interview sessions with theory, coding, and SQL tasks.

## Stack

- `backend`: FastAPI + SQLAlchemy + Alembic
- `frontend`: React + Vite + TypeScript
- `sandbox-code`: isolated Python code runner
- `sandbox-sql`: isolated SQL runner (uses `sqlite3` internally by design)
- Main backend database: PostgreSQL

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
- `WEB_SEARCH_URL=http://localhost:8003/search`
- `ALLOW_ORIGINS=*`

## Main API Groups

- `POST /sessions`, `GET /sessions/{id}`
- `GET/POST /sessions/{id}/messages`
- `POST /sessions/{id}/tasks/{task_id}/submit_code`
- `POST /sessions/{id}/tasks/{task_id}/submit_sql`
- `GET /sessions/{id}/lm/chat-stream`, `POST /sessions/{id}/lm/chat`
- `POST /sessions/{id}/practice/code`, `POST /sessions/{id}/practice/sql`
- CRUD: `/roles`, `/scenarios`, `/sql-scenarios`, `/rag/*`
