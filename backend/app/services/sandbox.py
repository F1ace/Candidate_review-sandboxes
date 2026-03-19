from typing import Any, Dict
import httpx
from ..config import settings


def run_code(language: str, code: str, tests: list[dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "language": language,
        "code": code,
        "tests": tests,
    }
    try:
        resp = httpx.post(settings.sandbox_code_url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {
            "success": False,
            "details": f"sandbox run_code failed: {exc}",
        }


def run_sql(sql: str, scenario_id: str) -> Dict[str, Any]:
    payload = {
        "sql": sql,
        "scenario_id": scenario_id,
    }
    try:
        resp = httpx.post(settings.sandbox_sql_url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {
            "success": False,
            "details": f"sandbox run_sql failed: {exc}",
        }