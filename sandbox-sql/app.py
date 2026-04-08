import sqlite3
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI(title="candidate-review sandbox-sql")


class RunSqlRequest(BaseModel):
    schema_sql: str = Field(default="", description="DDL для создания таблиц")
    seed_sql: Optional[str] = Field(default=None, description="опциональные INSERT'ы")
    query: str


class RunSqlResponse(BaseModel):
    success: bool
    columns: list[str] = []
    rows: list[list[Any]] = []
    error: Optional[str] = None

def _strip_sql_comments_and_separators(query: str) -> str:
    text = query or ""
    n = len(text)
    i = 0
    out: list[str] = []

    in_line_comment = False
    in_block_comment = False
    in_single_quote = False
    in_double_quote = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_single_quote:
            out.append(ch)
            if ch == "'" and nxt == "'":
                out.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single_quote = False
            i += 1
            continue

        if in_double_quote:
            out.append(ch)
            if ch == '"' and nxt == '"':
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_double_quote = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if ch == "'":
            in_single_quote = True
            out.append(ch)
            i += 1
            continue

        if ch == '"':
            in_double_quote = True
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    cleaned = "".join(out)
    return cleaned

def _has_executable_sql(query: str) -> bool:
    cleaned = _strip_sql_comments_and_separators(query)
    cleaned = cleaned.strip()

    if not cleaned:
        return False

    cleaned = cleaned.strip(";").strip()
    if not cleaned:
        return False

    return True

@app.post("/run_sql", response_model=RunSqlResponse)
def run_sql(req: RunSqlRequest) -> RunSqlResponse:
    con: Optional[sqlite3.Connection] = None
    try:
        if not _has_executable_sql(req.query):
            return RunSqlResponse(
                success=False,
                error="SQL query is empty or contains only comments",
            )

        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        if req.schema_sql.strip():
            cur.executescript(req.schema_sql)
        if req.seed_sql and req.seed_sql.strip():
            cur.executescript(req.seed_sql)

        cur.execute(req.query)

        if cur.description is None:
            con.commit()
            return RunSqlResponse(success=True)

        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        return RunSqlResponse(success=True, columns=cols, rows=rows)
    except Exception as exc:  # noqa: BLE001
        return RunSqlResponse(success=False, error=str(exc))
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
