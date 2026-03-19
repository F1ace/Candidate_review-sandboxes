from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

CODE_PIPELINE: tuple[str, ...] = (
    "run_code",
    "score_task",
)


def parse_run_code_report(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Извлекает нормализованный отчет из tool-result run_code.
    Ожидаемый контракт:
    {
        "ok": True,
        "task_id": "...",
        "result": {
            "success": bool,
            "tests_total": int,
            "tests_passed": int,
            "test_results": [...],
            "stdout": str,
            "stderr": str,
            "exit_code": int,
            "details": str | None,
        }
    }
    """
    if not isinstance(result, dict):
        return None

    if result.get("ok") is not True:
        return None

    payload = result.get("result")
    if not isinstance(payload, dict):
        return None

    test_results = payload.get("test_results") or []
    tests_total = payload.get("tests_total")
    if tests_total is None:
        tests_total = len(test_results)

    tests_passed = payload.get("tests_passed")
    if tests_passed is None:
        tests_passed = sum(1 for item in test_results if item.get("passed"))

    passrate = (float(tests_passed) / float(tests_total)) if tests_total else 0.0

    return {
        "success": bool(payload.get("success")),
        "tests_total": int(tests_total or 0),
        "tests_passed": int(tests_passed or 0),
        "passrate": passrate,
        "test_results": test_results,
        "stdout": payload.get("stdout") or "",
        "stderr": payload.get("stderr") or "",
        "exit_code": payload.get("exit_code"),
        "details": payload.get("details"),
    }

def build_practice_comment_template(
    *,
    tests_passed: int,
    tests_total: int,
    points: int,
    max_points: int,
) -> str:
    return (
        f"Итог: {points}/{max_points}.\n"
        f"Тесты: пройдено {tests_passed} из {tests_total}.\n"
        "Корректность: [заполни кратко на основе результатов sandbox и кода кандидата].\n"
        "Качество кода: [заполни кратко: читаемость, структура, нейминг, крайние случаи].\n"
        "Сложность и эффективность: [если применимо, оцени кратко; если несущественно — так и напиши].\n"
        "Что можно улучшить: [1-3 конкретных замечания]."
    )

def has_tool_error(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return "tool returned non-dict result"

    if result.get("ok") is False:
        return str(result.get("error") or "unknown tool error")

    return None

def normalize_practice_comment(
    raw_comment: str,
    *,
    tests_passed: int,
    tests_total: int,
    points: int,
    max_points: int,
) -> str:
    raw = (raw_comment or "").strip()

    sections_order = [
        "Корректность:",
        "Качество кода:",
        "Сложность и эффективность:",
        "Что можно улучшить:",
    ]

    defaults = {
        "Корректность:": "Корректность:",
        "Качество кода:": "Качество кода:",
        "Сложность и эффективность:": "Сложность и эффективность:",
        "Что можно улучшить:": "Что можно улучшить:",
    }

    if not raw:
        return "\n".join(defaults[h] for h in sections_order)

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    found: dict[str, str] = {}

    for line in lines:
        for header in sections_order:
            if line.startswith(header):
                found[header] = line
                break

    if not found:
        return "\n".join([
            f"Корректность: {raw}",
            defaults["Качество кода:"],
            defaults["Сложность и эффективность:"],
            defaults["Что можно улучшить:"],
        ])

    return "\n".join(found.get(h, defaults[h]) for h in sections_order)

@dataclass
class CodeWorkflowState:
    """Declarative workflow state for coding practice tool-calling."""

    max_points: float = 0.0
    completed: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def next_required_tool(self) -> str | None:
        for step in CODE_PIPELINE:
            if step not in self.completed:
                return step
        return None

    def allowed_tools(self) -> list[str]:
        nxt = self.next_required_tool()
        return [nxt] if nxt else []

    def is_complete(self) -> bool:
        return self.next_required_tool() is None

    def _complete(self, name: str) -> None:
        if name not in self.completed:
            self.completed.append(name)

    def mark_result(self, name: str, result: dict[str, Any]) -> tuple[bool, str | None]:
        err = has_tool_error(result)
        if err:
            return False, err

        if name == "run_code":
            report = parse_run_code_report(result)
            if report is not None:
                self.artifacts["run_report"] = report
                self._complete(name)
                return True, None
            return False, "run_code: structured result was not found"

        if name == "score_task":
            if isinstance(result, dict) and result.get("ok") is True:
                self.artifacts["score_result"] = result
                self._complete(name)
                return True, None
            return False, "score_task: tool result missing ok=true"

        return False, f"unsupported workflow tool: {name}"

    def prepare_args(
        self,
        name: str,
        args: dict[str, Any] | None,
        *,
        task_id: str,
        candidate_code: str,
    ) -> tuple[dict[str, Any], str | None]:
        payload = dict(args or {})

        if name in {"score_task", "run_code"}:
            payload["task_id"] = payload.get("task_id") or task_id

        if name == "run_code":
            # Для coding-проверки всегда используем исходный код кандидата из server-side context.
            # Модель не должна передавать или переписывать code/task_id/language для sandbox.
            payload["task_id"] = task_id
            payload["language"] = "python"
            payload["code"] = candidate_code
            return payload, None

        if name == "score_task":
            report = self.artifacts.get("run_report") or {}
            passrate = float(report.get("passrate") or 0.0)

            auto_points = int(round(self.max_points * passrate))
            tests_passed = int(report.get("tests_passed") or 0)
            tests_total = int(report.get("tests_total") or 0)
            max_points = int(round(self.max_points or 0))

            raw_points = payload.get("points", auto_points)
            try:
                points = int(round(float(raw_points)))
            except Exception:
                points = auto_points

            points = max(0, min(max_points, points))

            payload["task_id"] = task_id
            payload["points"] = points
            payload["is_final"] = True

            model_comment = (payload.get("comment") or "").strip()

            payload["comment"] = normalize_practice_comment(
                model_comment,
                tests_passed=tests_passed,
                tests_total=tests_total,
                points=points,
                max_points=max_points,
            )
            payload["run_code_result"] = report
            return payload, None

    def short_status(self) -> str:
        done = ", ".join(self.completed) if self.completed else "none"
        nxt = self.next_required_tool() or "none"
        return f"done=[{done}], next={nxt}"
