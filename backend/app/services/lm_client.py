from typing import Any, List, Optional

import httpx

from ..config import settings


class LMStudioClient:
    """Minimal client for LM Studio HTTP API compatible with OpenAI chat format."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url or settings.lm_studio_url
        self.client = httpx.Client(
            timeout=httpx.Timeout(
                connect=10.0,
                read=180.0,
                write=30.0,
                pool=30.0,
            )
        )

    def chat(
        self,
        messages: List[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        tool_choice: Any | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": settings.lm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        resp = self.client.post(self.base_url, json=payload)

        if resp.status_code >= 400:
            detail = resp.text
            raise httpx.HTTPStatusError(
                f"LM request failed with {resp.status_code}: {detail}",
                request=resp.request,
                response=resp,
            )

        return resp.json()

    def stream_chat(
        self,
        messages: List[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        tool_choice: Any | None = None,
    ):
        payload: dict[str, Any] = {
            "model": settings.lm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"

        with self.client.stream("POST", self.base_url, json=payload, timeout=120) as resp:
            if resp.status_code >= 400:
                detail = resp.read().decode(errors="ignore")
                raise httpx.HTTPStatusError(
                    f"LM stream request failed with {resp.status_code}: {detail}",
                    request=resp.request,
                    response=resp,
                )

            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode()
                if not isinstance(line, str):
                    continue
                if not line.startswith("data:"):
                    continue

                raw = line.split("data:", 1)[1].strip()
                if raw == "[DONE]":
                    break

                try:
                    import json as _json
                    parsed = _json.loads(raw)
                except Exception:
                    continue

                delta = parsed.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content

    def ping(self) -> dict[str, Any]:
        """Lightweight connectivity check to LM Studio."""
        payload = {"model": settings.lm_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        resp = self.client.post(self.base_url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()


lm_client = LMStudioClient()
