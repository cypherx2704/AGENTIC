"""Deeper SSE-streaming tests for POST /v1/chat/completions (stream=true).

Drives the ASGI app with ``mock_providers=true``, the same auth-dependency override
the existing chat test uses, and no DB (the usage-write path no-ops). Asserts the
SSE content-type, the presence of ``data: `` chunks, that the assembled delta content
reconstructs the mock reply, that a final chunk carries a ``usage`` with ``cost_usd``,
and that the stream terminates with ``[DONE]``.
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

# Force mock providers + a harmless DB URL before importing the app.
os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["llm:invoke"],
        principal_type="agent",
    )


@pytest_asyncio.fixture
async def client() -> AsyncClient:  # type: ignore[misc]
    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None  # no DB -> usage-write path no-ops
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _parse_sse_data_events(body: str) -> list[str]:
    """Return the raw payload string of every ``data: `` SSE line in order."""
    return [
        line[len("data: ") :].strip()
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_stream_content_type_and_done_terminator(client: AsyncClient) -> None:
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join([part async for part in resp.aiter_text()])

    # Body carries SSE `data: ` chunks and terminates with the sentinel.
    assert "data: " in body
    data_events = _parse_sse_data_events(body)
    assert data_events, "expected at least one SSE data event"
    assert data_events[-1] == "[DONE]"
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_assembled_content_and_usage_with_cost(client: AsyncClient) -> None:
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "hello there friend"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        body = "".join([part async for part in resp.aiter_text()])

    data_events = _parse_sse_data_events(body)
    assert data_events[-1] == "[DONE]"

    # Parse every non-sentinel data event as a chat.completion.chunk.
    chunks = [json.loads(ev) for ev in data_events if ev != "[DONE]"]
    assert chunks, "expected JSON chunks before [DONE]"
    for ch in chunks:
        assert ch["object"] == "chat.completion.chunk"

    # Reassemble the streamed delta content; the mock echoes the last user message.
    assembled = "".join(
        ch["choices"][0]["delta"].get("content", "")
        for ch in chunks
        if ch.get("choices")
    )
    assert "hello there friend" in assembled

    # A final chunk must carry usage with a positive cost_usd (Component 6).
    usage_chunks = [ch for ch in chunks if ch.get("usage")]
    assert usage_chunks, "expected a usage-bearing chunk before [DONE]"
    usage = usage_chunks[-1]["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["cost_usd"] > 0

    # The finish_reason on the terminal-content chunk is "stop".
    finish_reasons = [
        ch["choices"][0]["finish_reason"]
        for ch in chunks
        if ch.get("choices")
    ]
    assert "stop" in finish_reasons
