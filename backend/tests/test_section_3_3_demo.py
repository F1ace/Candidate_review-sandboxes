from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_sandbox_app(directory: str, module_name: str):
    sandbox_dir = REPO_ROOT / directory
    app_path = sandbox_dir / "app.py"
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load sandbox module from {app_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_tools_for_task_returns_only_allowed_actions():
    from app.routers.sessions_api.tools import tool_names, tools_for_task

    theory_no_rag = tool_names(
        tools_for_task({"type": "theory"}, rag_available=False)
    )
    theory_with_rag = tool_names(
        tools_for_task({"type": "theory"}, rag_available=True)
    )
    coding = tool_names(
        tools_for_task({"type": "coding"}, rag_available=False)
    )
    sql = tool_names(
        tools_for_task({"type": "sql"}, rag_available=False)
    )
    no_task = tools_for_task(None, rag_available=False)

    assert theory_no_rag == {"score_task"}

    assert theory_with_rag == {"rag_search", "score_task"}

    assert coding == {"run_code", "score_task"}

    assert sql == {"run_sql", "score_task"}

    assert no_task is None


def test_sandbox_code_executes_simple_python_solution():
    sandbox = _load_sandbox_app("sandbox-code", "_sandbox_code_app")
    client = TestClient(sandbox.app)

    candidate_code = (
        "def add(a, b):\n"
        "    return a + b\n"
    )

    payload = {
        "language": "python",
        "code": candidate_code,
        "tests": [
            {
                "code": candidate_code,
                "name": "adds two positive numbers",
                "language": "python",
                "input_data": {"args": [2, 3]},
                "expected_output": 5,
                "entrypoint_kind": "function",
                "entrypoint_name": "add",
            },
            {
                "code": candidate_code,
                "name": "adds zero",
                "language": "python",
                "input_data": {"args": [0, 7]},
                "expected_output": 7,
                "entrypoint_kind": "function",
                "entrypoint_name": "add",
            },
        ],
    }

    response = client.post("/run_code", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert data["tests_total"] == 2
    assert data["tests_passed"] == 2

    test_results = data["test_results"]
    assert len(test_results) == 2
    assert all(item.get("passed") is True for item in test_results)


def test_sandbox_sql_executes_query_against_in_memory_db():
    sandbox = _load_sandbox_app("sandbox-sql", "_sandbox_sql_app")
    client = TestClient(sandbox.app)

    payload = {
        "schema_sql": (
            "CREATE TABLE users (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    name TEXT NOT NULL,\n"
            "    age INTEGER NOT NULL\n"
            ");"
        ),
        "seed_sql": (
            "INSERT INTO users (id, name, age) VALUES\n"
            "    (1, 'Alice', 28),\n"
            "    (2, 'Bob', 35),\n"
            "    (3, 'Carol', 22);"
        ),
        "query": "SELECT name FROM users WHERE age >= 25 ORDER BY age;",
    }

    response = client.post("/run_sql", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert data["columns"] == ["name"]
    assert data["rows"] == [["Alice"], ["Bob"]]
    assert data.get("error") in (None, "")