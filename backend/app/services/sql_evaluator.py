from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from . import sandbox


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    return value


def _normalize_rows(rows: list[list[Any]]) -> list[list[Any]]:
    normalized: list[list[Any]] = []
    for row in rows or []:
        normalized.append([_normalize_scalar(v) for v in row])
    return normalized


def _sort_rows(rows: list[list[Any]]) -> list[list[Any]]:
    return sorted(rows, key=lambda r: tuple("" if v is None else str(v) for v in r))


def _compare_exact(
    candidate_columns: list[str],
    candidate_rows: list[list[Any]],
    expected_columns: list[str],
    expected_rows: list[list[Any]],
    order_sensitive: bool,
) -> dict[str, Any]:
    candidate_columns_norm = [str(c).strip() for c in (candidate_columns or [])]
    expected_columns_norm = [str(c).strip() for c in (expected_columns or [])]

    columns_match = candidate_columns_norm == expected_columns_norm

    candidate_rows_norm = _normalize_rows(candidate_rows or [])
    expected_rows_norm = _normalize_rows(expected_rows or [])

    if not order_sensitive:
        candidate_rows_norm = _sort_rows(candidate_rows_norm)
        expected_rows_norm = _sort_rows(expected_rows_norm)

    rows_match = candidate_rows_norm == expected_rows_norm
    is_correct = columns_match and rows_match

    feedback_parts: list[str] = []
    if columns_match:
        feedback_parts.append("Колонки результата совпадают с ожидаемыми.")
    else:
        feedback_parts.append(
            f"Колонки не совпадают. Ожидались {expected_columns_norm}, получены {candidate_columns_norm}."
        )

    if rows_match:
        feedback_parts.append("Строки результата совпадают с ожидаемыми.")
    else:
        feedback_parts.append("Строки результата не совпадают с ожидаемыми.")

    if is_correct:
        score_ratio = 1.0
    elif rows_match or columns_match:
        score_ratio = 0.5
    else:
        score_ratio = 0.0

    return {
        "is_correct": is_correct,
        "score_ratio": score_ratio,
        "feedback": " ".join(feedback_parts),
        "details": {
            "columns_match": columns_match,
            "rows_match": rows_match,
            "expected_columns": expected_columns_norm,
            "candidate_columns": candidate_columns_norm,
            "expected_rows": expected_rows_norm,
            "candidate_rows": candidate_rows_norm,
        },
    }


def _run_reference_query(schema_sql: str, query: str) -> dict[str, Any]:
    return sandbox.run_sql(
        schema_sql=schema_sql,
        seed_sql=None,
        query=query,
    )


def _evaluate_exact(
    *,
    scenario: models.SqlScenario,
    candidate_result: dict[str, Any],
    reference_cfg: dict[str, Any],
) -> dict[str, Any]:
    solution_queries = reference_cfg.get("solution_queries") or []
    if not solution_queries:
        return {
            "is_correct": False,
            "score_ratio": 0.0,
            "feedback": "В сценарии не задан solution_queries для exact-сравнения.",
            "details": {},
        }

    expected_columns = reference_cfg.get("expected_columns") or []
    order_sensitive = bool(reference_cfg.get("order_sensitive", True))
    ref_query = solution_queries[0]

    ref_result = _run_reference_query(scenario.db_schema or "", ref_query)
    if not ref_result.get("success"):
        return {
            "is_correct": False,
            "score_ratio": 0.0,
            "feedback": f"Эталонный SQL не выполнился: {ref_result.get('error')}",
            "details": {"reference_error": ref_result.get("error")},
        }

    return _compare_exact(
        candidate_columns=candidate_result.get("columns") or [],
        candidate_rows=candidate_result.get("rows") or [],
        expected_columns=expected_columns or (ref_result.get("columns") or []),
        expected_rows=ref_result.get("rows") or [],
        order_sensitive=order_sensitive,
    )


def _evaluate_post_state(
    *,
    scenario: models.SqlScenario,
    candidate_query: str,
    reference_cfg: dict[str, Any],
) -> dict[str, Any]:
    solution_queries = reference_cfg.get("solution_queries") or []
    validation_query = reference_cfg.get("validation_query")

    if not solution_queries:
        return {
            "is_correct": False,
            "score_ratio": 0.0,
            "feedback": "В сценарии не задан solution_queries для post_state-сравнения.",
            "details": {},
        }

    if not validation_query:
        return {
            "is_correct": False,
            "score_ratio": 0.0,
            "feedback": "В сценарии не задан validation_query для post_state-сравнения.",
            "details": {},
        }

    candidate_state = sandbox.run_sql(
        schema_sql=scenario.db_schema or "",
        seed_sql=None,
        query=f"{candidate_query.strip()}\n\n{validation_query}",
    )

    if not candidate_state.get("success"):
        return {
            "is_correct": False,
            "score_ratio": 0.0,
            "feedback": f"SQL кандидата не выполнился: {candidate_state.get('error')}",
            "details": {"candidate_error": candidate_state.get("error")},
        }

    reference_state = sandbox.run_sql(
        schema_sql=scenario.db_schema or "",
        seed_sql=None,
        query=f"{solution_queries[0].strip()}\n\n{validation_query}",
    )

    if not reference_state.get("success"):
        return {
            "is_correct": False,
            "score_ratio": 0.0,
            "feedback": f"Эталонный SQL не выполнился: {reference_state.get('error')}",
            "details": {"reference_error": reference_state.get("error")},
        }

    expected_columns = reference_cfg.get("expected_columns") or (reference_state.get("columns") or [])
    order_sensitive = bool(reference_cfg.get("order_sensitive", True))

    return _compare_exact(
        candidate_columns=candidate_state.get("columns") or [],
        candidate_rows=candidate_state.get("rows") or [],
        expected_columns=expected_columns,
        expected_rows=reference_state.get("rows") or [],
        order_sensitive=order_sensitive,
    )


def evaluate_sql_answer(
    *,
    db: Session,
    task_row: models.Task,
    query: str,
    execution_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scenario_name = (task_row.sql_scenario_ref or "").strip()
    if not scenario_name:
        return {
            "is_correct": False,
            "score": 0,
            "max_score": task_row.max_points or 0,
            "score_ratio": 0.0,
            "feedback": "У задачи не настроен sql_scenario_ref.",
            "details": {},
        }

    scenario = (
        db.query(models.SqlScenario)
        .filter(models.SqlScenario.name == scenario_name)
        .first()
    )
    if not scenario:
        return {
            "is_correct": False,
            "score": 0,
            "max_score": task_row.max_points or 0,
            "score_ratio": 0.0,
            "feedback": f"SQL scenario not found: {scenario_name}",
            "details": {},
        }

    reference_cfg = _safe_json(scenario.reference_solutions)
    compare_mode = (reference_cfg.get("compare_mode") or "exact").strip()

    if compare_mode == "post_state":
        eval_result = _evaluate_post_state(
            scenario=scenario,
            candidate_query=query,
            reference_cfg=reference_cfg,
        )
    else:
        exec_payload = execution_result or {}
        candidate_result = exec_payload.get("result") if "result" in exec_payload else exec_payload

        if not candidate_result or not candidate_result.get("success"):
            error_text = None
            if isinstance(candidate_result, dict):
                error_text = candidate_result.get("error")
            eval_result = {
                "is_correct": False,
                "score_ratio": 0.0,
                "feedback": f"SQL кандидата не выполнился: {error_text or 'unknown error'}",
                "details": {"candidate_error": error_text},
            }
        else:
            eval_result = _evaluate_exact(
                scenario=scenario,
                candidate_result=candidate_result,
                reference_cfg=reference_cfg,
            )

    max_score = int(task_row.max_points or 0)
    score = int(round(max_score * float(eval_result.get("score_ratio", 0.0))))

    return {
        "is_correct": bool(eval_result.get("is_correct")),
        "score_ratio": float(eval_result.get("score_ratio", 0.0)),
        "score": score,
        "max_score": max_score,
        "feedback": eval_result.get("feedback") or "",
        "details": eval_result.get("details") or {},
        "compare_mode": compare_mode,
        "sql_scenario_id": scenario_name,
    }