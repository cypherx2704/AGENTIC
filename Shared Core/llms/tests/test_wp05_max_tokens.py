"""WP05 max_tokens ceiling on POST /v1/chat/completions.

The model cap comes from ``services.capabilities`` (alias "smart" -> claude-sonnet-4-6,
cap 8192). Two policies, selected by ``settings.max_tokens_over_cap_policy``:

* "reject" (default) -> 400 VALIDATION_ERROR with details.reason == MAX_TOKENS_EXCEEDED.
* "clamp" -> the value is silently clamped to the cap and the response carries
  ``X-Cypherx-Param-Clamped: max_tokens``.

The clamp case flips the policy on ``app.state.settings`` (the live instance the chat
path reads) after startup, so no env / lru_cache mutation is needed and the change is
scoped to the one app instance.
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
from llms_gateway.services.capabilities import capability_registry  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
SONNET_CAP = 8192  # claude-sonnet-4-6 max_tokens_cap (cold-start fallback)


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id="00000000-0000-0000-0000-0000000000bb",
        scopes=["llm:invoke"],
        principal_type="agent",
    )


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        app.state.valkey = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


@pytest.mark.asyncio
async def test_cap_is_8192_for_smart() -> None:
    # Guards the test's premise: the alias target's cap is what we expect.
    cap = capability_registry.get("claude-sonnet-4-6")
    assert cap is not None and cap.max_tokens_cap == SONNET_CAP


@pytest.mark.asyncio
async def test_over_cap_rejected_400(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    assert app.state.settings.max_tokens_over_cap_policy == "reject"
    resp = await ac.post(
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": SONNET_CAP + 1,
        },
    )
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["reason"] == "MAX_TOKENS_EXCEEDED"
    assert err["details"]["max_tokens_cap"] == SONNET_CAP
    assert err["details"]["requested"] == SONNET_CAP + 1


@pytest.mark.asyncio
async def test_at_cap_allowed(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    resp = await ac.post(
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": SONNET_CAP,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Cypherx-Param-Clamped") is None


@pytest.mark.asyncio
async def test_clamp_policy_sets_header(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # Flip the live settings instance the chat path reads (scoped to this app).
    app.state.settings.max_tokens_over_cap_policy = "clamp"
    try:
        resp = await ac.post(
            "/v1/chat/completions",
            json={
                "model": "smart",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": SONNET_CAP + 5000,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("X-Cypherx-Param-Clamped") == "max_tokens"
    finally:
        app.state.settings.max_tokens_over_cap_policy = "reject"
