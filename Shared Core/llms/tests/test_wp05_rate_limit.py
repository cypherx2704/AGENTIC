"""WP05 per-tenant rate limiting for POST /v1/chat/completions.

Injects an in-memory fake Valkey on ``app.state.valkey``. The chat path resolves
limits via ``resolve_limits``; with the test Principal's empty ``raw_claims`` this
fails open to the ``default_plan`` ("free") tier: ``requests_per_min=60``,
``prompt_tokens_per_min=100_000``, ``completion_tokens_per_min=50_000``
(``services.auth_client._FALLBACK_LIMITS``).

Deterministic crossing of a limit without issuing 60+ calls: we pre-seed the
current fixed-window counters (computing the same window id the limiter uses) so a
SINGLE request crosses the threshold. Covers the request-count gate, the post-hoc
token gate, the ``Retry-After`` header, and the no-Valkey fail-open path. Also
asserts ``debit_tokens`` increments the per-minute token counters after a call.
"""

from __future__ import annotations

import os
import time

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.core.config import get_settings  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402
from llms_gateway.services.auth_client import _FALLBACK_LIMITS  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
FREE = _FALLBACK_LIMITS["free"]


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["llm:invoke"],
        principal_type="agent",
    )


class FakeValkey:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(self, key, value, *, ttl_seconds=None, timeout_seconds=None) -> None:  # type: ignore[no-untyped-def]
        self.store[key] = value

    async def set_if_absent(self, key, value, *, ttl_seconds, timeout_seconds=None) -> bool:  # type: ignore[no-untyped-def]
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def incr_with_expire(self, key, *, ttl_seconds, timeout_seconds=None) -> int:  # type: ignore[no-untyped-def]
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n

    async def incrby_with_expire(self, key, amount, *, ttl_seconds, timeout_seconds=None) -> int:  # type: ignore[no-untyped-def]
        n = int(self.store.get(key, "0")) + amount
        self.store[key] = str(n)
        return n


def _window_keys() -> tuple[str, str, str]:
    """(req, ptok, ctok) keys for the CURRENT fixed window — matches rate_limit._keys."""
    s = get_settings()
    window = int(time.time()) // s.rate_limit_window_seconds
    p = s.rate_limit_key_prefix
    return (
        f"{p}req:{TEST_TENANT}:{window}",
        f"{p}ptok:{TEST_TENANT}:{window}",
        f"{p}ctok:{TEST_TENANT}:{window}",
    )


@pytest_asyncio.fixture
async def make_client():  # type: ignore[no-untyped-def]
    managers: list = []

    async def _factory(valkey: object | None):  # type: ignore[no-untyped-def]
        app = create_app()
        app.dependency_overrides[require_principal] = _fake_principal
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append(lm)
        app.state.db_pool = None
        app.state.valkey = valkey
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        await ac.__aenter__()
        managers.append(ac)
        return app, ac

    yield _factory

    for m in reversed(managers):
        await m.__aexit__(None, None, None)


_CHAT = {"model": "smart", "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.asyncio
async def test_request_count_over_limit_429_with_retry_after(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    app, ac = await make_client(valkey)
    req_key, _p, _c = _window_keys()
    # Seed the request counter AT the limit; the next INCR -> limit+1 -> reject.
    valkey.store[req_key] = str(FREE.requests_per_min)

    resp = await ac.post("/v1/chat/completions", json=_CHAT)
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["details"]["dimension"] == "requests"
    # Retry-After header present and a positive integer second count.
    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None
    assert int(retry_after) >= 1


@pytest.mark.asyncio
async def test_prompt_token_budget_over_limit_429(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    app, ac = await make_client(valkey)
    _r, ptok_key, _c = _window_keys()
    # A prior heavy minute already blew the prompt-token budget -> next request rejected.
    valkey.store[ptok_key] = str(FREE.prompt_tokens_per_min + 1)

    resp = await ac.post("/v1/chat/completions", json=_CHAT)
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["details"]["dimension"] == "prompt_tokens"
    assert resp.headers.get("Retry-After") is not None


@pytest.mark.asyncio
async def test_under_limit_allows_and_debits_tokens(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    app, ac = await make_client(valkey)
    req_key, ptok_key, ctok_key = _window_keys()

    resp = await ac.post("/v1/chat/completions", json=_CHAT)
    assert resp.status_code == 200, resp.text
    usage = resp.json()["usage"]

    # enforce_pre incremented the request counter once.
    assert int(valkey.store[req_key]) == 1
    # debit_tokens added the consumed prompt/completion tokens post-hoc.
    assert int(valkey.store[ptok_key]) == usage["prompt_tokens"]
    assert int(valkey.store[ctok_key]) == usage["completion_tokens"]


@pytest.mark.asyncio
async def test_no_valkey_fail_open_allows(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(None)
    resp = await ac.post("/v1/chat/completions", json=_CHAT)
    assert resp.status_code == 200, resp.text  # no Valkey -> never 429
