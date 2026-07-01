"""WP05 streaming correctness — consolidated tool_calls + normalized finish_reason.

The mock provider is a test double; it emits OpenAI-shape per-index ``tool_calls``
deltas (id+name on the first fragment, arguments streamed incrementally) followed by
ONE consolidated terminal ``tool_calls[]`` event with ``finish_reason: "tool_calls"``
and a usage chunk — mirroring the real providers' Component-6 aggregation. This drives
the chat path's streaming-correctness wiring (consume loop + ``_finalize_stream``
usage path) end-to-end without a live provider.

Asserts:
* The terminal SSE event carries a consolidated ``tool_calls[]`` (single entry, full
  reassembled arguments) and a normalized ``finish_reason == "tool_calls"``.
* The terminal event also carries a ``usage`` with a positive ``cost_usd`` — i.e. the
  usage-record / debit path (``_finalize_stream``) is reachable.
* The stream still terminates with ``[DONE]``.
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id="00000000-0000-0000-0000-0000000000bb",
        scopes=["llm:invoke"],
        principal_type="agent",
    )


@pytest_asyncio.fixture
async def client() -> AsyncClient:  # type: ignore[misc]
    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        app.state.valkey = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _data_events(body: str) -> list[str]:
    return [
        line[len("data: ") :].strip()
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "search the web",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]


@pytest.mark.asyncio
async def test_stream_consolidates_tool_calls_and_normalizes_finish_reason(client: AsyncClient) -> None:
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "find something"}],
            "tools": _TOOLS,
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join([part async for part in resp.aiter_text()])

    events = _data_events(body)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(ev) for ev in events if ev != "[DONE]"]
    assert chunks

    # The terminal (usage-bearing) chunk consolidates the tool call and normalizes
    # the finish_reason to "tool_calls".
    terminal = next(ch for ch in chunks if ch.get("usage"))
    choice = terminal["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tcs = choice["delta"]["tool_calls"]
    assert len(tcs) == 1
    tc = tcs[0]
    assert tc["id"] == "call_mock_0"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "search"
    # The arguments were reassembled into the full JSON string.
    assert json.loads(tc["function"]["arguments"]) == {"q": "hi"}

    # The usage-record path is reached: positive cost on the terminal usage chunk.
    usage = terminal["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["cost_usd"] > 0


@pytest.mark.asyncio
async def test_stream_without_tools_finishes_stop(client: AsyncClient) -> None:
    # Regression: the tool-call branch must NOT alter the plain text-stream path.
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "say hi"}], "stream": True},
    ) as resp:
        assert resp.status_code == 200
        body = "".join([part async for part in resp.aiter_text()])

    events = _data_events(body)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(ev) for ev in events if ev != "[DONE]"]
    finish_reasons = [
        ch["choices"][0]["finish_reason"] for ch in chunks if ch.get("choices")
    ]
    assert "stop" in finish_reasons
    assert "tool_calls" not in finish_reasons
    assert all("tool_calls" not in (ch["choices"][0]["delta"]) for ch in chunks if ch.get("choices"))
