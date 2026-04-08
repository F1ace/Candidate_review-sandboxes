from __future__ import annotations

import httpx

from app import models
from app.routers.sessions_api.prompting import _build_system_prompt
from app.services.lm_client import LMStudioClient


class _FakeHttpClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, json: dict, timeout=None):  # noqa: ANN001
        self.calls.append(json)
        return self._responses.pop(0)


def _response(status_code: int, *, text: str | None = None, json_body: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "http://lm.test/v1/chat/completions")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, text=text or "", request=request)


def test_lm_client_retries_with_compacted_messages_on_context_overflow() -> None:
    client = LMStudioClient(base_url="http://lm.test/v1/chat/completions")
    fake_client = _FakeHttpClient(
        [
            _response(
                400,
                text=(
                    '{"error":"The number of tokens to keep from the initial prompt is greater '
                    'than the context length (n_keep: 14120>= n_ctx: 4096)."}'
                ),
            ),
            _response(200, json_body={"id": "ok", "choices": [{"message": {"content": "ready"}}]}),
        ]
    )
    client.client = fake_client

    messages = [
        {"role": "system", "content": "A" * 6000},
        {"role": "system", "content": "B" * 4000},
        {"role": "assistant", "content": "C" * 2500},
        {"role": "user", "content": "D" * 2200},
    ]

    payload = client.chat(messages)

    assert payload["id"] == "ok"
    assert len(fake_client.calls) == 2

    first_messages = fake_client.calls[0]["messages"]
    second_messages = fake_client.calls[1]["messages"]
    first_size = sum(len(str(item.get("content") or "")) for item in first_messages)
    second_size = sum(len(str(item.get("content") or "")) for item in second_messages)

    assert second_size < first_size
    assert len(second_messages) <= len(first_messages)


def test_system_prompt_stays_compact_for_large_scenario_payload(db_session) -> None:
    role = models.Role(name="Backend", slug="backend", description="role")
    db_session.add(role)
    db_session.flush()

    long_question = " ".join(["Explain idempotency and HTTP semantics in detail."] * 40)
    long_brief = " ".join(["Design and implement a service with retries, caching, and observability."] * 60)

    scenario = models.Scenario(
        role_id=role.id,
        name="Large scenario",
        slug="large-scenario",
        description="stress",
        difficulty="middle",
        tasks=[
            {
                "id": "T1",
                "type": "theory",
                "title": "Theory block",
                "max_points": 10,
                "questions": [long_question for _ in range(8)],
            },
            {
                "id": "C1",
                "type": "coding",
                "title": "Coding block",
                "max_points": 10,
                "description": long_brief,
            },
        ],
        config={},
    )
    db_session.add(scenario)
    db_session.commit()
    db_session.refresh(scenario)

    session = models.Session(
        scenario_id=scenario.id,
        role_id=role.id,
        state="active",
        current_task_id="T1",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    prompt = _build_system_prompt(session, rag_available=True)

    assert len(prompt) < 5000
    assert "Текущий task id: T1" in prompt
    assert "run_code" in prompt
    assert "rag_search" in prompt
