"""End-to-end-ish test of POST /v1/chat/completions against the mock provider.

Runs httpx against the ASGI app with ``mock_providers=true`` and overrides the auth
dependency to inject a fixed Principal — so no real Auth / JWKS / Kafka is needed.
The DB pool is left absent so the usage-write path no-ops (asserted shape + cost only).
"""

from __future__ import annotations

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
    # Drop the DB pool so the usage-write path no-ops (unit test, no DB). Generous
    # startup timeout since the lifespan attempts a (bounded) DB warm-up first.
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_chat_completion_mock_shape_and_cost(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello there"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "Hello there" in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"

    usage = body["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["cost_usd"] > 0


@pytest.mark.asyncio
async def test_reserved_metadata_key_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"tenant_id": "spoofed"},
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "reserved_metadata_key"


@pytest.mark.asyncio
async def test_streaming_returns_sse_with_usage(client: AsyncClient) -> None:
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
        chunks = [line async for line in resp.aiter_lines()]
    joined = "\n".join(chunks)
    assert "data: [DONE]" in joined
    assert '"usage"' in joined


@pytest.mark.asyncio
async def test_livez_ok() -> None:
    app = create_app()
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/livez")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_aggregate_tool_calls_false_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "hi"}],
            "stream_options": {"aggregate_tool_calls": False},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MODEL_UNSUPPORTED"
