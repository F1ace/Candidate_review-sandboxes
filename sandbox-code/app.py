import importlib.util
import tempfile
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI(title="candidate-review sandbox-code")


class TestCasePayload(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    language: str = "python"
    input_data: Optional[dict[str, Any]] = None
    expected_output: Any = None
    checker_source: Optional[str] = None
    expected_error: Optional[str] = None
    entrypoint_kind: Optional[str] = None
    entrypoint_name: Optional[str] = None
    method_name: Optional[str] = None


class RunCodeRequest(BaseModel):
    language: str = Field(default="python")
    code: str
    tests: list[TestCasePayload] = Field(default_factory=list)


class RunCodeResponse(BaseModel):
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    details: Optional[str] = None
    tests_total: int = 0
    tests_passed: int = 0
    test_results: list[dict[str, Any]] = Field(default_factory=list)


def _load_module(path: str):
    spec = importlib.util.spec_from_file_location("candidate_main", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

def _resolve_saved_args(saved_values: dict[str, Any], keys: list[str]) -> list[Any]:
    result = []
    for key in keys:
        if key not in saved_values:
            raise KeyError(f"Saved value '{key}' not found")
        result.append(saved_values[key])
    return result

from dataclasses import asdict, is_dataclass
from math import isclose

def _normalize_value(value: Any):
    if is_dataclass(value):
        return asdict(value)

    if isinstance(value, tuple):
        # Приводим tuple к list, потому что через JSON tuple всё равно приезжает как array/list
        return [_normalize_value(v) for v in value]

    if isinstance(value, list):
        return [_normalize_value(v) for v in value]

    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}

    return value


def _values_equal(actual: Any, expected: Any, *, float_tol: float = 1e-6) -> bool:
    actual = _normalize_value(actual)
    expected = _normalize_value(expected)

    # float ~= float
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return isclose(float(actual), float(expected), rel_tol=float_tol, abs_tol=float_tol)

    # list vs list
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return False
        return all(_values_equal(a, e, float_tol=float_tol) for a, e in zip(actual, expected))

    # dict subset semantics:
    # expected может содержать только те поля, которые мы хотим проверить
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key, expected_value in expected.items():
            if key not in actual:
                return False
            if not _values_equal(actual[key], expected_value, float_tol=float_tol):
                return False
        return True

    return actual == expected

def _run_custom_checker(
    checker_source: str,
    *,
    actual: Any,
    expected: Any,
    saved_values: dict[str, Any] | None = None,
) -> bool:
    scope: dict[str, Any] = {}
    exec(checker_source, scope, scope)
    check = scope.get("check")
    if not callable(check):
        raise ValueError("checker_source must define function check(actual, expected, saved_values)")
    return bool(check(actual, expected, saved_values or {}))

def _install_monotonic_mock(module: Any, sequence: list[float]):
    if not hasattr(module, "time"):
        raise ValueError("candidate module does not import time, cannot patch monotonic")

    iterator = iter(sequence)

    def fake_monotonic():
        try:
            return next(iterator)
        except StopIteration:
            return sequence[-1]

    original = module.time.monotonic
    module.time.monotonic = fake_monotonic
    return original

def _run_function_case(module: Any, test: TestCasePayload) -> dict[str, Any]:
    input_data = test.input_data or {}
    expected_output = test.expected_output
    saved_values: dict[str, Any] = {}

    fn_name = test.entrypoint_name
    if not fn_name:
        raise ValueError("entrypoint_name is missing")

    fn = getattr(module, fn_name, None)
    if fn is None:
        raise AttributeError(f"Function '{fn_name}' not found")

    args = input_data.get("args", [])
    kwargs = input_data.get("kwargs", {})

    try:
        actual = fn(*args, **kwargs)
        actual_normalized = _normalize_value(actual)
        expected_normalized = _normalize_value(expected_output)

        if test.expected_error:
            return {
                "passed": False,
                "actual": actual_normalized,
                "expected": {"error": test.expected_error},
                "validation_mode": "expected_error",
            }

        if test.checker_source:
            passed = _run_custom_checker(
                test.checker_source,
                actual=actual_normalized,
                expected=expected_normalized,
                saved_values=saved_values,
            )
            validation_mode = "custom_checker"
        else:
            passed = _values_equal(actual_normalized, expected_normalized)
            validation_mode = "exact"

        return {
            "passed": passed,
            "actual": actual_normalized,
            "expected": expected_normalized,
            "validation_mode": validation_mode,
        }

    except Exception as exc:
        if test.expected_error:
            passed = exc.__class__.__name__ == test.expected_error
            return {
                "passed": passed,
                "actual": {"error": exc.__class__.__name__, "message": str(exc)},
                "expected": {"error": test.expected_error},
                "validation_mode": "expected_error",
            }
        raise

def _run_class_case(module: Any, test: TestCasePayload) -> dict[str, Any]:
    input_data = test.input_data or {}
    expected_output = test.expected_output

    class_name = test.entrypoint_name
    if not class_name:
        raise ValueError("entrypoint_name is missing")

    cls = getattr(module, class_name, None)
    if cls is None:
        raise AttributeError(f"Class '{class_name}' not found")

    original_monotonic = None
    monotonic_sequence = input_data.get("monotonic_sequence")
    if monotonic_sequence:
        original_monotonic = _install_monotonic_mock(module, monotonic_sequence)

    try:
        constructor = input_data.get("constructor") or {}
        ctor_args = constructor.get("args", [])
        ctor_kwargs = constructor.get("kwargs", {})

        obj = cls(*ctor_args, **ctor_kwargs)

        saved_values: dict[str, Any] = {"__self__": obj}
        calls = input_data.get("calls") or []
        last_result = None

        for call in calls:
            method_name = call.get("method")
            if not method_name:
                raise ValueError("Each class call must contain 'method'")

            method = getattr(obj, method_name, None)
            if method is None:
                raise AttributeError(f"Method '{method_name}' not found on class '{class_name}'")

            if "args_from_saved" in call:
                args = _resolve_saved_args(saved_values, call.get("args_from_saved") or [])
            else:
                args = call.get("args", [])

            kwargs = call.get("kwargs", {})
            last_result = method(*args, **kwargs)

            save_as = call.get("save_as")
            if save_as:
                saved_values[save_as] = last_result

            save_index_as = call.get("save_index_as") or {}
            if isinstance(save_index_as, dict):
                for save_key, index in save_index_as.items():
                    if not isinstance(last_result, (list, tuple)):
                        raise ValueError(f"Cannot save index from non-sequence result for key '{save_key}'")
                    saved_values[save_key] = last_result[index]

        actual = last_result
        expected = expected_output

        actual_normalized = _normalize_value(actual)
        expected_normalized = _normalize_value(expected)

        if test.checker_source:
            passed = _run_custom_checker(
                test.checker_source,
                actual=actual_normalized,
                expected=expected_normalized,
                saved_values=saved_values,
            )
            validation_mode = "custom_checker"
        else:
            passed = _values_equal(actual_normalized, expected_normalized)
            validation_mode = "exact"

        return {
            "passed": passed,
            "actual": actual_normalized,
            "expected": expected_normalized,
            "validation_mode": validation_mode,
        }

    except Exception as exc:
        if test.expected_error:
            passed = exc.__class__.__name__ == test.expected_error
            return {
                "passed": passed,
                "actual": {"error": exc.__class__.__name__, "message": str(exc)},
                "expected": {"error": test.expected_error},
                "validation_mode": "expected_error",
            }
        raise
    finally:
        if original_monotonic is not None:
            module.time.monotonic = original_monotonic

def _run_python_tests(code: str, tests: list[TestCasePayload]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        path = f"{td}/main.py"
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)

        try:
            module = _load_module(path)
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Import error: {exc}",
                "exit_code": 1,
                "details": "Candidate code could not be imported",
                "tests_total": len(tests),
                "tests_passed": 0,
                "test_results": [],
            }

        results = []
        passed = 0

        for test in tests:
            try:
                if test.entrypoint_kind == "function":
                    case_result = _run_function_case(module, test)
                elif test.entrypoint_kind == "class":
                    case_result = _run_class_case(module, test)
                else:
                    raise ValueError(
                        f"Unsupported entrypoint_kind: {test.entrypoint_kind}. "
                        "Supported kinds: function, class"
                    )

                ok = bool(case_result["passed"])
                if ok:
                    passed += 1

                results.append({
                    "code": test.code,
                    "name": test.name,
                    "description": test.description,
                    "passed": ok,
                    "actual": case_result.get("actual"),
                    "expected": case_result.get("expected"),
                    "error": None,
                    "validation_mode": case_result.get("validation_mode", "exact"),
                })

            except Exception as exc:
                results.append({
                    "code": test.code,
                    "name": test.name,
                    "description": test.description,
                    "passed": False,
                    "actual": None,
                    "expected": None,
                    "error": str(exc),
                })

        return {
            "success": passed == len(tests) if tests else True,
            "stdout": "",
            "stderr": "",
            "exit_code": 0 if passed == len(tests) else 1,
            "details": None,
            "tests_total": len(tests),
            "tests_passed": passed,
            "test_results": results,
        }


@app.post("/run_code", response_model=RunCodeResponse)
def run_code(req: RunCodeRequest) -> RunCodeResponse:
    lang = (req.language or "python").lower().strip()
    if lang not in {"python", "py"}:
        return RunCodeResponse(
            success=False,
            exit_code=2,
            stderr=f"Unsupported language: {req.language}. MVP supports only python.",
        )

    result = _run_python_tests(req.code, req.tests)
    return RunCodeResponse(**result)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
