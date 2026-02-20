import subprocess
import tempfile
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI(title="candidate-review sandbox-code")


class RunCodeRequest(BaseModel):
    language: str = Field(default="python")
    code: str
    tests_id: Optional[str] = None


class RunCodeResponse(BaseModel):
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    details: Optional[str] = None


def _run_python(code: str, timeout_s: int = 20) -> dict[str, Any]:
    """MVP executor.

    Изоляция достигается тем, что сервис запускается в отдельном контейнере.
    Здесь дополнительно ограничиваем время выполнения.
    """

    with tempfile.TemporaryDirectory() as td:
        path = f"{td}/main.py"
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)

        try:
            proc = subprocess.run(
                ["python", path],
                cwd=td,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            return {
                "success": proc.returncode == 0,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "exit_code": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stdout": "",
                "stderr": "TimeoutExpired",
                "exit_code": 124,
                "details": f"Execution exceeded {timeout_s}s",
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

    result = _run_python(req.code)
    return RunCodeResponse(**result)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
