from typing import Any, List, Optional

import httpx

from ..config import settings


_CONTEXT_OVERFLOW_MARKERS = (
    "n_keep",
    "n_ctx",
    "context length",
    "maximum context length",
    "prompt is too long",
)


def _is_context_overflow_error(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def _estimate_messages_size(messages: List[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("role") or ""))
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(str(content))
    return total


def _trim_message_content(content: Any, limit: int) -> Any:
    if not isinstance(content, str):
        return content
    if len(content) <= limit:
        return content

    head = max(0, int(limit * 0.65))
    tail = max(0, limit - head - 20)
    if tail <= 0:
        return content[:limit]
    return f"{content[:head].rstrip()}\n...[truncated]...\n{content[-tail:].lstrip()}"


def _message_limit(role: str, aggressive: bool) -> int:
    if role == "system":
        return 1200 if aggressive else 2200
    if role == "user":
        return 900 if aggressive else 1500
    if role == "assistant":
        return 900 if aggressive else 1400
    if role == "tool":
        return 700 if aggressive else 1000
    return 800 if aggressive else 1200


def _compact_messages(messages: List[dict[str, Any]], aggressive: bool = False) -> List[dict[str, Any]]:
    if not messages:
        return messages

    preserved_head: list[dict[str, Any]] = []
    head_index = 0
    while head_index < len(messages) and len(preserved_head) < 2 and messages[head_index].get("role") == "system":
        message = dict(messages[head_index])
        message["content"] = _trim_message_content(message.get("content"), _message_limit("system", aggressive))
        preserved_head.append(message)
        head_index += 1

    tail_source = messages[head_index:]
    tail_count = 6 if aggressive else 10
    preserved_tail: list[dict[str, Any]] = []
    for message in tail_source[-tail_count:]:
        compacted = dict(message)
        role = str(compacted.get("role") or "")
        compacted["content"] = _trim_message_content(compacted.get("content"), _message_limit(role, aggressive))
        preserved_tail.append(compacted)

    compacted_messages = preserved_head + preserved_tail
    if not compacted_messages:
        compacted_messages = messages[-tail_count:]

    return compacted_messages


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

    def _build_payload(
        self,
        messages: List[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]],
        temperature: float,
        tool_choice: Any | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": settings.lm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if stream:
            payload["stream"] = True
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        return payload

    def _raise_http_error(self, resp: httpx.Response, prefix: str) -> None:
        detail = resp.text
        raise httpx.HTTPStatusError(
            f"{prefix} with {resp.status_code}: {detail}",
            request=resp.request,
            response=resp,
        )

    def _post_with_context_retry(
        self,
        *,
        messages: List[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        temperature: float,
        tool_choice: Any | None,
    ) -> httpx.Response:
        attempts = [messages]
        compacted = _compact_messages(messages, aggressive=False)
        if _estimate_messages_size(compacted) < _estimate_messages_size(messages):
            attempts.append(compacted)

        aggressive = _compact_messages(messages, aggressive=True)
        if _estimate_messages_size(aggressive) < _estimate_messages_size(attempts[-1]):
            attempts.append(aggressive)

        last_response: httpx.Response | None = None
        for index, attempt_messages in enumerate(attempts):
            payload = self._build_payload(
                attempt_messages,
                tools=tools,
                temperature=temperature,
                tool_choice=tool_choice,
            )
            response = self.client.post(self.base_url, json=payload)
            if response.status_code < 400:
                return response

            last_response = response
            if not _is_context_overflow_error(response.text) or index == len(attempts) - 1:
                break

        assert last_response is not None
        return last_response

    def chat(
        self,
        messages: List[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        tool_choice: Any | None = None,
    ) -> dict[str, Any]:
        resp = self._post_with_context_retry(
            messages=messages,
            tools=tools,
            temperature=temperature,
            tool_choice=tool_choice,
        )

        if resp.status_code >= 400:
            self._raise_http_error(resp, "LM request failed")

        return resp.json()

    def stream_chat(
        self,
        messages: List[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        tool_choice: Any | None = None,
    ):
        payload = self._build_payload(
            _compact_messages(messages, aggressive=False),
            tools=tools,
            temperature=temperature,
            tool_choice=tool_choice,
            stream=True,
        )

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
        payload = {
            "model": settings.lm_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        resp = self.client.post(self.base_url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()


lm_client = LMStudioClient()
