"""Unit tests for the fail-open Valkey-backed idempotency replay + rate limiter.

These exercise the service functions directly against an in-memory Valkey fake
(mirroring ``tests.conftest.FakeValkey``) so no live Valkey is required. They assert
the REAL behavior of the current code: idempotency store/replay round-trips, unknown
keys read back ``None``, the rate limiter allows under the limit and raises a 429
``ApiError`` over it, and BOTH services fail open (no raise / allow) when Valkey is
absent or errors.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_principal
from tool_flow_bridge.core.config import get_settings
from tool_flow_bridge.core.errors import ApiError, ErrorCode
from tool_flow_bridge.services import idempotency, rate_limit

SCOPE = "sum-tool"


class FakeValkey:
    """In-memory Valkey stand-in with the narrow command surface the services call."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(  # type: ignore[no-untyped-def]
        self, key, value, *, ttl_seconds=None, timeout_seconds=None
    ) -> None:
        self.store[key] = value

    async def incr_with_expire(  # type: ignore[no-untyped-def]
        self, key, *, ttl_seconds, timeout_seconds=None
    ) -> int:
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n


class BrokenValkey:
    """Valkey stand-in whose every command RAISES — drives the fail-open branches."""

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        raise ConnectionError("valkey down")

    async def set(  # type: ignore[no-untyped-def]
        self, key, value, *, ttl_seconds=None, timeout_seconds=None
    ) -> None:
        raise ConnectionError("valkey down")

    async def incr_with_expire(  # type: ignore[no-untyped-def]
        self, key, *, ttl_seconds, timeout_seconds=None
    ) -> int:
        raise ConnectionError("valkey down")


# ── Idempotency ────────────────────────────────────────────────────────────────


async def test_idempotency_store_then_get_replay_returns_payload() -> None:
    valkey = FakeValkey()
    principal = make_principal()
    body = {"tool": "add", "result": {"sum": 5}}

    await idempotency.store(
        valkey, "abc-123", principal, 200, body, scope=SCOPE
    )

    replay = await idempotency.get_replay(valkey, "abc-123", principal, scope=SCOPE)

    assert isinstance(replay, idempotency.StoredResponse)
    assert replay.status_code == 200
    assert replay.body == body


async def test_get_replay_unknown_key_returns_none() -> None:
    valkey = FakeValkey()
    principal = make_principal()

    replay = await idempotency.get_replay(valkey, "never-stored", principal, scope=SCOPE)

    assert replay is None


async def test_get_replay_none_key_is_inert() -> None:
    valkey = FakeValkey()
    principal = make_principal()

    assert await idempotency.get_replay(valkey, None, principal, scope=SCOPE) is None


async def test_get_replay_none_valkey_returns_none() -> None:
    principal = make_principal()

    assert await idempotency.get_replay(None, "abc-123", principal, scope=SCOPE) is None


async def test_idempotency_is_tenant_and_scope_scoped() -> None:
    valkey = FakeValkey()
    principal = make_principal()
    body = {"ok": True}

    await idempotency.store(valkey, "k1", principal, 201, body, scope="scope-a")

    # Same key, different scope -> miss (record keys are scope-qualified).
    assert await idempotency.get_replay(valkey, "k1", principal, scope="scope-b") is None
    # Same key + same scope -> hit.
    hit = await idempotency.get_replay(valkey, "k1", principal, scope="scope-a")
    assert hit is not None and hit.status_code == 201


async def test_get_replay_fails_open_when_valkey_errors() -> None:
    principal = make_principal()

    # Broken Valkey on read -> fail open (no replay), never raises.
    assert (
        await idempotency.get_replay(BrokenValkey(), "abc-123", principal, scope=SCOPE)
        is None
    )


async def test_store_swallows_valkey_errors() -> None:
    principal = make_principal()

    # Best-effort store must not raise even when the backend is down.
    await idempotency.store(
        BrokenValkey(), "abc-123", principal, 200, {"x": 1}, scope=SCOPE
    )


# ── Rate limiting ────────────────────────────────────────────────────────────────


async def test_enforce_allows_under_the_limit() -> None:
    valkey = FakeValkey()
    principal = make_principal()

    # limit=3: the first three calls in a window are allowed (no raise).
    for _ in range(3):
        await rate_limit.enforce(valkey, principal, dimension="requests", limit=3)


async def test_enforce_blocks_over_the_limit() -> None:
    valkey = FakeValkey()
    principal = make_principal()

    await rate_limit.enforce(valkey, principal, dimension="requests", limit=2)
    await rate_limit.enforce(valkey, principal, dimension="requests", limit=2)

    with pytest.raises(ApiError) as exc_info:
        await rate_limit.enforce(valkey, principal, dimension="requests", limit=2)

    err = exc_info.value
    assert err.code == ErrorCode.RATE_LIMIT_EXCEEDED
    assert err.status_code == 429
    assert err.details == {
        "dimension": "requests",
        "limit": 2,
        "retry_after_seconds": err.details["retry_after_seconds"],
    }
    assert err.details["retry_after_seconds"] >= 1
    assert err.headers is not None
    assert err.headers["Retry-After"] == str(err.details["retry_after_seconds"])


async def test_enforce_fails_open_when_no_valkey_client() -> None:
    principal = make_principal()

    # No client -> ALLOW (fail open), even with a limit of 0 that would otherwise reject.
    await rate_limit.enforce(None, principal, dimension="requests", limit=1)


async def test_enforce_fails_open_when_valkey_errors() -> None:
    principal = make_principal()

    # Backend raises on INCR -> fail open (allow), no ApiError propagated.
    await rate_limit.enforce(BrokenValkey(), principal, dimension="requests", limit=1)


async def test_enforce_disabled_is_noop() -> None:
    principal = make_principal()
    settings = get_settings()
    disabled = settings.model_copy(update={"rate_limit_enabled": False})

    # Would reject on the 2nd call if enabled; disabled -> always allowed.
    await rate_limit.enforce(FakeValkey(), principal, limit=0, settings=disabled)
