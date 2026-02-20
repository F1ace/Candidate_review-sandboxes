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


@app.post("/run_sql", response_model=RunSqlResponse)
def run_sql(req: RunSqlRequest) -> RunSqlResponse:
    con: Optional[sqlite3.Connection] = None
    try:
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
