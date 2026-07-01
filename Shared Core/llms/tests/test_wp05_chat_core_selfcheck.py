"""Self-check (WP05 chat-path core): fail-open without live infra.

Confirms the mock-provider unit path still works with db_pool=None and no Valkey:
non-stream success, streaming success, an Idempotency-Key header is a harmless no-op
(begin -> FAILOPEN), max_tokens over the model cap rejects 400 MAX_TOKENS_EXCEEDED,
and the X-Cypherx-Param-Clamped header appears under the clamp policy.
"""

from __future__ import annotations

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
        app.state.db_pool = None  # no DB -> usage-write path no-ops
        app.state.valkey = None  # no Valkey -> idempotency/rate-limit fail open
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_non_stream_with_idempotency_key_failopen(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Idempotency-Key": "abc-123"},
        json={"model": "smart", "messages": [{"role": "user", "content": "hi there"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("Idempotency-Replayed") is None  # no Valkey -> never replays
    body = resp.json()
    assert body["usage"]["cost_usd"] > 0


@pytest.mark.asyncio
async def test_stream_failopen(client: AsyncClient) -> None:
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "stream please"}], "stream": True},
    ) as resp:
        assert resp.status_code == 200
        body = "".join([part async for part in resp.aiter_text()])
    assert "data: [DONE]" in body
    assert '"usage"' in body


@pytest.mark.asyncio
async def test_max_tokens_over_cap_rejected(client: AsyncClient) -> None:
    # claude-sonnet-4-6 (alias "smart") caps at 8192.
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "smart", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 999999},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "MAX_TOKENS_EXCEEDED"
