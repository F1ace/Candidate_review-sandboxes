from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

CODE_PIPELINE: tuple[str, ...] = (
    "run_code",
    "score_task",
)

SQL_PIPELINE: tuple[str, ...] = (
    "run_sql",
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

def parse_run_sql_report(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Извлекает нормализованный отчет из tool-result run_sql.
    Ожидаемый контракт:
    {
        "ok": True,
        "task_id": "...",
        "result": {
            "success": bool,
            "columns": [...],
            "rows": [...],
            "error": str | None,
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

    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    error = payload.get("error")
    success = payload.get("success")

    if success is None:
        success = error in (None, "", False)

    return {
        "success": bool(success),
        "columns": columns if isinstance(columns, list) else [],
        "rows": rows if isinstance(rows, list) else [],
        "row_count": len(rows) if isinstance(rows, list) else 0,
        "error": str(error).strip() if error not in (None, "") else None,
    }

def build_practice_comment_template(
    *,
    tests_passed: int,
    tests_total: int,
    points: int,
    max_points: int,
) -> str:
    return (
        f"Корректность: По результатам sandbox пройдено {tests_passed} из {tests_total} тестов, "
        "поэтому итог зависит от фактической корректности решения на проверенных кейсах.\n"
        "Качество кода: Комментарий должен кратко оценивать читаемость, структуру, нейминг и обработку крайних случаев.\n"
        "Сложность и эффективность: Нужна короткая оценка сложности; если отдельные замечания несущественны, это можно явно отметить.\n"
        "Что можно улучшить: Нужно перечислить 1-3 конкретных улучшения без шаблонных фраз."
    )

def build_sql_practice_comment_template(
    *,
    points: int,
    max_points: int,
) -> str:
    return (
        "Корректность: Комментарий должен кратко описывать, насколько SQL-запрос корректен по результату выполнения и логике.\n"
        "Качество решения: Нужна короткая оценка структуры запроса, читаемости и уместности конструкции.\n"
        "Работа с SQL: Нужно отметить использование фильтрации, join, group by, оконных функций или агрегаций, если они есть.\n"
        "Что можно улучшить: Нужно перечислить 1-3 конкретных улучшения без шаблонных фраз."
    )


def _collect_failed_test_details(test_results: list[dict[str, Any]] | None, *, limit: int = 2) -> list[str]:
    details: list[str] = []
    for test in test_results or []:
        if test.get("passed"):
            continue
        name = _sanitize_practice_comment_text(str(test.get("name") or test.get("code") or "неизвестный тест").strip())
        error = _sanitize_practice_comment_text(str(test.get("error") or "").strip())
        if error:
            details.append(f"{name}: {error}")
        else:
            details.append(name)
        if len(details) >= limit:
            break
    return details


def _sanitize_practice_comment_text(text: str) -> str:
    cleaned = str(text or "")
    cleaned = cleaned.replace("[", "(").replace("]", ")")
    return " ".join(cleaned.split()).strip()


def _practice_comment_has_placeholders(comment: str) -> bool:
    lowered = (comment or "").lower()
    forbidden_markers = [
        "[",
        "]",
        "заполни",
        "если применимо",
        "1-3 конкретных замечания",
    ]
    return any(marker in lowered for marker in forbidden_markers)


def _practice_comment_is_valid(comment: str, headers: list[str]) -> bool:
    raw = (comment or "").strip()
    if not raw or _practice_comment_has_placeholders(raw):
        return False

    missing = [header for header in headers if header not in raw]
    if missing:
        return False

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    section_values: dict[str, str] = {}

    for line in lines:
        for header in headers:
            if line.startswith(header):
                value = line[len(header):].strip()
                section_values[header] = value
                break

    return all(section_values.get(header) for header in headers)


def build_practice_comment_from_report(
    *,
    report: dict[str, Any] | None,
    points: int,
    max_points: int,
) -> str:
    data = report or {}
    tests_passed = int(data.get("tests_passed") or 0)
    tests_total = int(data.get("tests_total") or 0)
    failed_details = _collect_failed_test_details(data.get("test_results") or [])

    if tests_total and tests_passed == tests_total:
        correctness = (
            f"Решение прошло все {tests_total} тестов sandbox, поэтому базовая логика задачи реализована корректно."
        )
        quality = (
            "По результатам прогона явных проблем, мешающих выполнению, не видно; код всё равно стоит держать читаемым и аккуратно структурированным."
        )
        complexity = (
            "Отдельных критичных замечаний по сложности и эффективности по данным sandbox не видно; приоритетом остаётся поддерживаемость решения."
        )
        improvements = (
            "Можно добавить ещё пару собственных тестов на крайние случаи и при необходимости чуть яснее оформить имена переменных и промежуточных шагов."
        )
    else:
        if tests_total:
            correctness = (
                f"Сейчас решение прошло {tests_passed} из {tests_total} тестов sandbox, поэтому логика реализована неполно и требует доработки."
            )
        else:
            correctness = (
                "Корректность решения не подтверждена тестами sandbox, поэтому считать задачу выполненной полностью пока нельзя."
            )

        if failed_details:
            correctness += " Наиболее заметные проблемы проявились в кейсах: " + "; ".join(failed_details) + "."

        quality = (
            "По текущему результату видно, что решение пока недостаточно устойчиво на проверочных кейсах; стоит упростить проблемные участки и отдельно проверить обработку крайних случаев."
        )
        complexity = (
            "Основной риск сейчас связан не с асимптотикой, а с корректностью логики; после исправления ошибок полезно ещё раз оценить поведение решения на граничных сценариях."
        )
        improvements = (
            "Исправьте причины падения тестов, добавьте локальные проверки на проблемные кейсы и убедитесь, что код явно обрабатывает граничные условия без скрытых допущений."
        )

    return "\n".join(
        [
            f"Корректность: {correctness}",
            f"Качество кода: {quality}",
            f"Сложность и эффективность: {complexity}",
            f"Что можно улучшить: {improvements}",
        ]
    ).strip()


def build_sql_practice_comment_from_report(
    *,
    report: dict[str, Any] | None,
    points: int,
    max_points: int,
) -> str:
    data = report or {}
    success = bool(data.get("success"))
    row_count = int(data.get("row_count") or 0)
    error = str(data.get("error") or "").strip()
    columns = [str(item).strip() for item in (data.get("columns") or []) if str(item).strip()]

    if success:
        correctness = (
            f"Запрос выполнился успешно и вернул {row_count} строк; базовая корректность решения подтверждается результатом выполнения."
        )
        quality = (
            "Структура решения читается последовательно; при этом всегда полезно следить за явностью фильтров, алиасов и порядка вычислений."
        )
        sql_quality = (
            "Используемые SQL-конструкции выглядят уместно для задачи; набор колонок и агрегаций стоит дополнительно сверять с ожидаемой бизнес-логикой."
        )
        improvements = (
            "Можно добавить явную сортировку результата, при необходимости уточнить алиасы колонок и перепроверить поведение на пустых или граничных данных."
        )
        if columns:
            sql_quality += " В результате возвращаются колонки: " + ", ".join(columns[:6]) + "."
    else:
        correctness = (
            "Запрос не выполнился корректно, поэтому решение пока нельзя считать рабочим."
        )
        if error:
            correctness += f" Sandbox вернул ошибку: {error}."
        quality = (
            "Структуру запроса стоит упростить и перепроверить по частям, чтобы быстрее локализовать проблемный фрагмент."
        )
        sql_quality = (
            "Нужно отдельно перепроверить синтаксис и логику используемых SQL-конструкций: фильтрацию, соединения, агрегации и соответствие выбранных полей задаче."
        )
        improvements = (
            "Исправьте ошибку выполнения, проверьте запрос на небольшом наборе данных и затем отдельно убедитесь, что итоговые поля и агрегаты совпадают с ожидаемым результатом."
        )

    return "\n".join(
        [
            f"Корректность: {correctness}",
            f"Качество решения: {quality}",
            f"Работа с SQL: {sql_quality}",
            f"Что можно улучшить: {improvements}",
        ]
    ).strip()

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
    if not raw:
        return ""

    sections_order = [
        "Корректность:",
        "Качество кода:",
        "Сложность и эффективность:",
        "Что можно улучшить:",
    ]

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    found: dict[str, list[str]] = {}
    current_header: str | None = None

    for line in lines:
        matched_header = None

        for header in sections_order:
            if line.startswith(header):
                matched_header = header
                tail = line[len(header):].strip()
                found.setdefault(header, [])
                if tail:
                    found[header].append(tail)
                current_header = header
                break

        if matched_header is not None:
            continue

        if current_header is not None:
            found.setdefault(current_header, []).append(line)

    if not found:
        return raw

    normalized_lines: list[str] = []
    for header in sections_order:
        if header in found:
            value = " ".join(found[header]).strip()
            if value:
                normalized_lines.append(f"{header} {value}")
            else:
                normalized_lines.append(header)

    return "\n".join(normalized_lines).strip()

def normalize_sql_practice_comment(
    raw_comment: str,
    *,
    points: int,
    max_points: int,
) -> str:
    raw = (raw_comment or "").strip()
    if not raw:
        return ""

    sections_order = [
        "Корректность:",
        "Качество решения:",
        "Работа с SQL:",
        "Что можно улучшить:",
    ]

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    found: dict[str, list[str]] = {}
    current_header: str | None = None

    for line in lines:
        matched_header = None

        for header in sections_order:
            if line.startswith(header):
                matched_header = header
                tail = line[len(header):].strip()
                found.setdefault(header, [])
                if tail:
                    found[header].append(tail)
                current_header = header
                break

        if matched_header is not None:
            continue

        if current_header is not None:
            found.setdefault(current_header, []).append(line)

    if not found:
        return raw

    normalized_lines: list[str] = []
    for header in sections_order:
        if header in found:
            value = " ".join(found[header]).strip()
            if value:
                normalized_lines.append(f"{header} {value}")
            else:
                normalized_lines.append(header)

    return "\n".join(normalized_lines).strip()

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
            normalized_comment = normalize_practice_comment(
                model_comment,
                tests_passed=tests_passed,
                tests_total=tests_total,
                points=points,
                max_points=max_points,
            )
            if not _practice_comment_is_valid(
                normalized_comment,
                [
                    "Корректность:",
                    "Качество кода:",
                    "Сложность и эффективность:",
                    "Что можно улучшить:",
                ],
            ):
                normalized_comment = build_practice_comment_from_report(
                    report=report,
                    points=points,
                    max_points=max_points,
                )

            payload["comment"] = normalized_comment
            payload["run_code_result"] = report
            return payload, None

    def short_status(self) -> str:
        done = ", ".join(self.completed) if self.completed else "none"
        nxt = self.next_required_tool() or "none"
        return f"done=[{done}], next={nxt}"

@dataclass
class SqlWorkflowState:
    max_points: float = 0.0
    completed: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def next_required_tool(self) -> str | None:
        for step in SQL_PIPELINE:
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

        if name == "run_sql":
            report = parse_run_sql_report(result)
            if report is not None:
                self.artifacts["run_report"] = report
                self._complete(name)
                return True, None
            return False, "run_sql: structured result was not found"

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
        candidate_query: str,
    ) -> tuple[dict[str, Any], str | None]:
        payload = dict(args or {})

        if name in {"run_sql", "score_task"}:
            payload["task_id"] = payload.get("task_id") or task_id

        if name == "run_sql":
            payload["task_id"] = task_id
            payload["query"] = candidate_query
            return payload, None

        if name == "score_task":
            report = self.artifacts.get("run_report")
            if not report:
                return {}, "run_sql must complete before score_task"

            max_points = int(round(self.max_points or 0))

            raw_points = payload.get("points", max_points)
            try:
                points = int(round(float(raw_points)))
            except Exception:
                points = 0

            points = max(0, min(max_points, points))

            payload["task_id"] = task_id
            payload["points"] = points
            payload["is_final"] = True

            model_comment = (payload.get("comment") or "").strip()
            normalized_comment = normalize_sql_practice_comment(
                model_comment,
                points=points,
                max_points=max_points,
            )
            if not _practice_comment_is_valid(
                normalized_comment,
                [
                    "Корректность:",
                    "Качество решения:",
                    "Работа с SQL:",
                    "Что можно улучшить:",
                ],
            ):
                normalized_comment = build_sql_practice_comment_from_report(
                    report=report,
                    points=points,
                    max_points=max_points,
                )

            payload["comment"] = normalized_comment
            payload["run_sql_result"] = report
            return payload, None

        return payload, None

    def short_status(self) -> str:
        done = ", ".join(self.completed) if self.completed else "none"
        nxt = self.next_required_tool() or "none"
        return f"done=[{done}], next={nxt}"
