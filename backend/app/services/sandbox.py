from typing import Any, Dict, Optional

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


def run_sql(
    *,
    schema_sql: str,
    query: str,
    seed_sql: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "schema_sql": schema_sql or "",
        "seed_sql": seed_sql,
        "query": query,
    }
    try:
        resp = httpx.post(settings.sandbox_sql_url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {
            "success": False,
            "error": f"sandbox run_sql failed: {exc}",
        }