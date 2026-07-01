"""Regression tests for three contract/robustness bugs (additive, deterministic).

All three exercise the public ASGI surface with ``mock_providers=true`` + the standard
``require_principal`` override (no Auth / JWKS / Kafka), ``db_pool=None`` so usage-writes
no-op — identical wiring to ``test_chat_mock`` / ``test_rerank`` / ``test_wp06_byok``.

1. Custom pydantic ValueError validators must return a proper Contract-2 **422** (not a
   generic 500). Previously ``RequestValidationError.errors()`` carried the raw ValueError
   under ``ctx.error`` and crashed the 422 handler's own ``json.dumps``. Covers the chat
   ``stop`` (>4 sequences) and tool ``function.name`` regex validators.
2. ``POST /v1/rerank`` with NO ``model`` must fall back to ``rerank_default_model`` (200),
   not 422 — the model field is now Optional so the endpoint default is reachable.
3. ``DELETE /v1/keys/{id}`` (and rotate) with a non-UUID id must return a clean **404**,
   not a 500 from psycopg's uuid cast.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform"
)

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.core.config import Settings  # noqa: E402
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


def _admin_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["llm:invoke", "tenant:admin"],
        principal_type="agent",
    )


@pytest_asyncio.fixture
async def client() -> AsyncClient:  # type: ignore[misc]
    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None  # usage-write no-ops
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ── BUG#1: custom ValueError validators -> 422 (not 500), with leaked-free detail ──
@pytest.mark.asyncio
async def test_stop_over_four_returns_422_not_500(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["a", "b", "c", "d", "e"],  # >4 -> custom ValueError validator
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    # The validator's message is surfaced in the (now JSON-safe) details.
    errors = body["error"]["details"]["errors"]
    assert any("at most 4 sequences" in str(e.get("msg", "")) for e in errors)


@pytest.mark.asyncio
async def test_tool_name_regex_returns_422_not_500(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "bad name!", "description": "x"},  # fails regex
                }
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    errors = body["error"]["details"]["errors"]
    assert any("function name must match" in str(e.get("msg", "")) for e in errors)


# ── BUG#2: rerank with no model falls back to the default (200, not 422) ───────────
@pytest.mark.asyncio
async def test_rerank_without_model_uses_default(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/rerank",
        json={"query": "brown fox", "documents": [{"text": "the quick brown fox"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Resolved to the configured default rerank model (echoed back).
    assert body["model"]
    assert isinstance(body["results"], list) and len(body["results"]) == 1


@pytest.mark.asyncio
async def test_rerank_null_model_uses_default(client: AsyncClient) -> None:
    # Explicit JSON null is treated identically to an omitted field -> falls back to the
    # default. (An empty string "" still 422s on min_length=1, which is intended: the
    # Optional fix only makes omission/null reach the endpoint default, not "".)
    resp = await client.post(
        "/v1/rerank",
        json={"model": None, "query": "q", "documents": [{"text": "x"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"]


# ── BUG#3: DELETE/rotate /v1/keys with a non-UUID id -> 404 (not 500) ──────────────
@pytest_asyncio.fixture
async def keys_client() -> AsyncClient:  # type: ignore[misc]
    app = create_app()
    app.dependency_overrides[require_principal] = _admin_principal
    async with LifespanManager(app, startup_timeout=15):
        # A non-None pool so _get_pool passes; the uuid guard fires before any SQL, so
        # the pool is never actually touched. Use a sentinel that explodes if queried.
        class _ExplodingPool:
            async def connection(self, **_k):  # noqa: ANN001, ANN201
                raise AssertionError("DB must not be touched for a non-UUID key id")

        app.state.db_pool = _ExplodingPool()
        app.state.valkey = None
        app.state.settings = Settings(byok_kek="a-test-key-encryption-passphrase-32+chars")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_delete_non_uuid_key_returns_404(keys_client: AsyncClient) -> None:
    resp = await keys_client.delete("/v1/keys/not-a-uuid")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_rotate_non_uuid_key_returns_404(keys_client: AsyncClient) -> None:
    resp = await keys_client.post(
        "/v1/keys/not-a-uuid/rotate", json={"secret": "sk-new"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
