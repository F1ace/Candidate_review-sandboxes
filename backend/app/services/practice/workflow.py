from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

CODE_PIPELINE: tuple[str, ...] = (
    "run_code",
    "score_task",
)


def parse_run_code_report(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract harness RESULT_JSON payload from sandbox run_code result."""
    if not isinstance(result, dict):
        return None

    stdout = (result.get("stdout") or "").strip()
    if not stdout:
        return None

    marker = "RESULT_JSON:"
    if marker in stdout:
        payload = stdout.split(marker, 1)[1].strip()
        try:
            return json.loads(payload)
        except Exception:
            return None

    if stdout.startswith("{") and stdout.endswith("}"):
        try:
            return json.loads(stdout)
        except Exception:
            return None

    return None


def has_tool_error(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return "tool returned non-dict result"
    if result.get("error"):
        return str(result.get("error"))
    return None


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
            return False, "run_code: RESULT_JSON not found in stdout"

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

        # Общие поля для tools
        if name in {"score_task", "run_code"}:
            payload["task_id"] = payload.get("task_id") or task_id

        if name == "run_code":
            # Пока нет harness/тестов — запускается код кандидата как есть.
            # Позже заменить это на server-side harness + тесты из БД.
            payload["language"] = payload.get("language") or "python"
            payload["code"] = payload.get("code") or candidate_code
            return payload, None

        if name == "score_task":
            report = self.artifacts.get("run_report") or {}
            passrate = float(report.get("passrate") or 0.0)
            payload["points"] = payload.get("points", round(self.max_points * passrate, 2))
            payload["comment"] = payload.get("comment") or (
                "Оценка выставлена автоматически по результатам выполнения кода "
                "(пока без тест-кейсов из БД)."
            )
            return payload, None

        return payload, f"unsupported workflow tool: {name}"

    def short_status(self) -> str:
        done = ", ".join(self.completed) if self.completed else "none"
        nxt = self.next_required_tool() or "none"
        return f"done=[{done}], next={nxt}"
