"""WP05 idempotency for POST /v1/chat/completions (Contract-9).

Injects an in-memory fake Valkey on ``app.state.valkey`` that satisfies the
``ValkeyClient`` command surface the idempotency service uses (``set_if_absent`` /
``get`` / ``set``). Deterministic, no live infra (db_pool=None so the usage-write
path no-ops).

Covers:
* First non-stream POST with an ``Idempotency-Key`` -> 200 and stores the response.
* A second identical POST -> replays the SAME body with ``Idempotency-Replayed: true``.
* A request while the key is ``in_flight`` -> 409 IDEMPOTENCY_REQUEST_IN_FLIGHT.
* No Valkey (None) -> fail-open (no 409 / no replay; the request proceeds 200).
* Streams (``stream=true``) are NOT stored or replayed.
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
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["llm:invoke"],
        principal_type="agent",
    )


class FakeValkey:
    """In-memory stand-in for ValkeyClient (no TTL expiry simulation needed here)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.store[key] = value

    async def set_if_absent(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int,
        timeout_seconds: float | None = None,
    ) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def incr_with_expire(
        self, key: str, *, ttl_seconds: int, timeout_seconds: float | None = None
    ) -> int:
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n

    async def incrby_with_expire(
        self, key: str, amount: int, *, ttl_seconds: int, timeout_seconds: float | None = None
    ) -> int:
        n = int(self.store.get(key, "0")) + amount
        self.store[key] = str(n)
        return n


def _build_app(valkey: object | None):  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    return app


@pytest_asyncio.fixture
async def make_client():  # type: ignore[no-untyped-def]
    """Returns a factory that yields (app, client) wired with a given valkey."""
    managers: list = []

    async def _factory(valkey: object | None):  # type: ignore[no-untyped-def]
        app = _build_app(valkey)
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append(lm)
        app.state.db_pool = None
        app.state.valkey = valkey
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        managers.append(ac)
        await ac.__aenter__()
        return app, ac

    yield _factory

    for m in reversed(managers):
        if isinstance(m, AsyncClient):
            await m.__aexit__(None, None, None)
        else:
            await m.__aexit__(None, None, None)


_CHAT = {"model": "smart", "messages": [{"role": "user", "content": "hi there"}]}


@pytest.mark.asyncio
async def test_first_call_stores_then_replays(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    app, ac = await make_client(valkey)

    first = await ac.post("/v1/chat/completions", headers={"Idempotency-Key": "k-1"}, json=_CHAT)
    assert first.status_code == 200, first.text
    assert first.headers.get("Idempotency-Replayed") is None
    first_body = first.json()

    # A completed record must now exist for tenant+key.
    keys = list(valkey.store)
    assert any(k.endswith(f"{TEST_TENANT}:k-1") for k in keys)
    stored = json.loads(next(valkey.store[k] for k in keys if k.endswith(":k-1")))
    assert stored["state"] == "completed"
    assert stored["stream"] is False

    # Second identical POST -> replay the SAME body with the header set.
    second = await ac.post("/v1/chat/completions", headers={"Idempotency-Key": "k-1"}, json=_CHAT)
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json() == first_body  # byte-for-byte identical replay


@pytest.mark.asyncio
async def test_in_flight_returns_409(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    app, ac = await make_client(valkey)

    # Simulate a concurrent duplicate: a prior request already claimed the slot and is
    # still running (in_flight marker present, not yet completed).
    from llms_gateway.core.config import get_settings

    s = get_settings()
    record_key = f"{s.idempotency_key_prefix}{TEST_TENANT}:k-busy"
    valkey.store[record_key] = json.dumps({"state": "in_flight", "stream": False})

    resp = await ac.post(
        "/v1/chat/completions", headers={"Idempotency-Key": "k-busy"}, json=_CHAT
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_REQUEST_IN_FLIGHT"


@pytest.mark.asyncio
async def test_no_valkey_fail_open(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(None)  # no Valkey wired

    first = await ac.post("/v1/chat/completions", headers={"Idempotency-Key": "k-x"}, json=_CHAT)
    assert first.status_code == 200, first.text
    assert first.headers.get("Idempotency-Replayed") is None

    # A second call also proceeds fresh (no replay, no 409) — fail-open.
    second = await ac.post("/v1/chat/completions", headers={"Idempotency-Key": "k-x"}, json=_CHAT)
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotency-Replayed") is None


@pytest.mark.asyncio
async def test_stream_not_stored_or_replayed(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    app, ac = await make_client(valkey)

    payload = {**_CHAT, "stream": True}
    async with ac.stream(
        "POST", "/v1/chat/completions", headers={"Idempotency-Key": "k-stream"}, json=payload
    ) as resp:
        assert resp.status_code == 200
        body = "".join([part async for part in resp.aiter_text()])
    assert "data: [DONE]" in body

    # No idempotency record was written for a stream (chat path skips begin/complete
    # entirely when body.stream is true).
    assert not any(k.endswith(":k-stream") for k in valkey.store)

    # A second streamed call with the same key is NOT a replay — it streams again.
    async with ac.stream(
        "POST", "/v1/chat/completions", headers={"Idempotency-Key": "k-stream"}, json=payload
    ) as resp2:
        assert resp2.status_code == 200
        assert resp2.headers.get("Idempotency-Replayed") is None
        body2 = "".join([part async for part in resp2.aiter_text()])
    assert "data: [DONE]" in body2
