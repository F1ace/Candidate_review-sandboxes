from typing import Any, Dict

import httpx

from ..config import settings
from ..database import SessionLocal
from .. import models


def run_code(language: str, code: str, tests_id: str) -> Dict[str, Any]:
    payload = {"language": language, "code": code, "tests_id": tests_id}
    try:
        resp = httpx.post(settings.sandbox_code_url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "details": f"sandbox run_code failed: {exc}"}


def run_sql(sql_scenario_id: str, query: str) -> Dict[str, Any]:
    # MVP: sandbox-sql не имеет доступа к БД backend, поэтому передаём DDL напрямую.
    schema_sql = ""
    seed_sql = None
    try:
        with SessionLocal() as db:
            scenario = db.get(models.SqlScenario, int(sql_scenario_id))
            if scenario and scenario.db_schema:
                schema_sql = scenario.db_schema
            # На будущее: seed_sql можно хранить в reference_solutions или отдельном поле.
    except Exception:
        # Не блокируем выполнение, просто отправим пустую схему.
        pass

    payload = {"schema_sql": schema_sql, "seed_sql": seed_sql, "query": query}
    try:
        resp = httpx.post(settings.sandbox_sql_url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"sandbox run_sql failed: {exc}"}
