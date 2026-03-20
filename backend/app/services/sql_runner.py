from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .. import models
from . import sandbox


def _normalize_sql_result(raw: dict[str, Any] | None, scenario_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "sql_scenario_id": scenario_name,
            "result": {
                "success": False,
                "columns": [],
                "rows": [],
                "error": "sandbox returned non-dict result",
            },
        }

    return {
        "ok": True,
        "sql_scenario_id": scenario_name,
        "result": {
            "success": bool(raw.get("success")),
            "columns": raw.get("columns") or [],
            "rows": raw.get("rows") or [],
            "error": raw.get("error"),
        },
    }


def run_sql_for_scenario_name(
    *,
    db: Session,
    scenario_name: str,
    query: str,
) -> dict[str, Any]:
    scenario = (
        db.query(models.SqlScenario)
        .filter(models.SqlScenario.name == scenario_name)
        .first()
    )
    if not scenario:
        return {
            "ok": False,
            "error": f"SQL scenario not found: {scenario_name}",
        }

    raw = sandbox.run_sql(
        schema_sql=scenario.db_schema or "",
        seed_sql=None,
        query=query,
    )
    return _normalize_sql_result(raw, scenario_name)


def run_sql_for_task(
    *,
    db: Session,
    task_row: models.Task,
    query: str,
) -> dict[str, Any]:
    scenario_name = (task_row.sql_scenario_ref or "").strip()
    if not scenario_name:
        return {
            "ok": False,
            "task_id": task_row.external_id,
            "error": f"Task has no sql_scenario_ref: {task_row.external_id}",
        }

    result = run_sql_for_scenario_name(
        db=db,
        scenario_name=scenario_name,
        query=query,
    )
    result["task_id"] = task_row.external_id
    return result