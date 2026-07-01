"""WP08 — Auth layer-B authorize (task:execute) on POST /v1/tasks.

Two layers under test:
  1. ``AuthClient.authorize`` directly (the unit owning the cache + fail-open posture):
       * cached deny  -> FORBIDDEN, no Auth call (cache hit avoids the round-trip);
       * cached allow -> returns, no Auth call;
       * cache miss   -> calls Auth POST /v1/authorize, caches the verdict;
       * Auth/Valkey error -> FAIL-OPEN (accepts).
  2. The HTTP submit path (``_authorize_submit`` wired into POST /v1/tasks):
       * a definitive deny surfaces as 403; an allow proceeds.

The Auth HTTP call is respx-mocked. The verdict cache is a small in-memory fake
implementing ``get_authorize_verdict`` / ``set_authorize_verdict`` (the methods the
client calls); a fake WITHOUT them models the "no cache" read-through case.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from agent_runtime.core.config import get_settings
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.services.auth_client import AuthClient
from agent_runtime.services.service_token import ServiceTokenProvider

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
TEST_AGENT_JWT = "test.inbound.agent-jwt"


class FakeAuthzCache:
    """Verdict-cache double implementing the authorize-cache helpers the client calls."""

    def __init__(self, *, seed: bool | None = None, raise_on_get: Exception | None = None) -> None:
        self._verdict = seed
        self.raise_on_get = raise_on_get
        self.set_calls: list[bool] = []
        self.get_calls = 0

    async def get_authorize_verdict(
        self, *, prefix: str, tenant_id: str, agent_id: str, action: str, timeout_seconds: float
    ) -> bool | None:
        self.get_calls += 1
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self._verdict

    async def set_authorize_verdict(
        self,
        *,
        prefix: str,
        tenant_id: str,
        agent_id: str,
        action: str,
        allowed: bool,
        ttl_seconds: int,
        timeout_seconds: float,
    ) -> None:
        self.set_calls.append(allowed)
        self._verdict = allowed


def _mock_service_token(router: respx.Router) -> None:
    s = get_settings()
    router.post(f"{s.auth_service_url.rstrip('/')}/v1/service-tokens").mock(
        return_value=httpx.Response(200, json={"access_token": "svc.jwt", "expires_in": 300})
    )


def _authorize_route() -> str:
    return f"{get_settings().auth_service_url.rstrip('/')}/v1/authorize"


def _make_client() -> tuple[AuthClient, ServiceTokenProvider]:
    settings = get_settings()
    tokens = ServiceTokenProvider(settings)
    return AuthClient(settings, tokens), tokens


# ── cache hit avoids the Auth call ──────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_cached_allow_skips_auth_call() -> None:
    cache = FakeAuthzCache(seed=True)
    route = respx.post(_authorize_route())  # registered but must NOT be hit
    auth, tokens = _make_client()
    try:
        await auth.authorize(
            tenant_id=TEST_TENANT,
            agent_id=TEST_AGENT,
            action="task:execute",
            agent_jwt=TEST_AGENT_JWT,
            valkey=cache,
        )
    finally:
        await auth.aclose()
        await tokens.aclose()

    assert cache.get_calls == 1
    assert not route.called  # cache hit -> no Auth round-trip


@pytest.mark.asyncio
@respx.mock
async def test_cached_deny_raises_forbidden_without_auth_call() -> None:
    cache = FakeAuthzCache(seed=False)
    route = respx.post(_authorize_route())
    auth, tokens = _make_client()
    try:
        with pytest.raises(ApiError) as ei:
            await auth.authorize(
                tenant_id=TEST_TENANT,
                agent_id=TEST_AGENT,
                action="task:execute",
                agent_jwt=TEST_AGENT_JWT,
                valkey=cache,
            )
        assert ei.value.code == ErrorCode.FORBIDDEN
    finally:
        await auth.aclose()
        await tokens.aclose()
    assert not route.called  # cached deny -> no Auth round-trip


# ── cache miss -> Auth call + verdict cached ────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_cache_miss_calls_auth_and_caches_allow() -> None:
    _mock_service_token(respx.mock)
    route = respx.post(_authorize_route()).mock(
        return_value=httpx.Response(200, json={"allowed": True})
    )
    cache = FakeAuthzCache(seed=None)  # miss
    auth, tokens = _make_client()
    try:
        await auth.authorize(
            tenant_id=TEST_TENANT,
            agent_id=TEST_AGENT,
            action="task:execute",
            agent_jwt=TEST_AGENT_JWT,
            valkey=cache,
        )
    finally:
        await auth.aclose()
        await tokens.aclose()

    assert route.called
    assert cache.set_calls == [True]  # the allow verdict was cached


@pytest.mark.asyncio
@respx.mock
async def test_auth_deny_body_raises_forbidden_and_caches_deny() -> None:
    _mock_service_token(respx.mock)
    respx.post(_authorize_route()).mock(
        return_value=httpx.Response(200, json={"decision": "deny"})
    )
    cache = FakeAuthzCache(seed=None)
    auth, tokens = _make_client()
    try:
        with pytest.raises(ApiError) as ei:
            await auth.authorize(
                tenant_id=TEST_TENANT,
                agent_id=TEST_AGENT,
                action="task:execute",
                agent_jwt=TEST_AGENT_JWT,
                valkey=cache,
            )
        assert ei.value.code == ErrorCode.FORBIDDEN
    finally:
        await auth.aclose()
        await tokens.aclose()
    assert cache.set_calls == [False]  # deny cached for the TTL window


@pytest.mark.asyncio
@respx.mock
async def test_auth_403_status_is_definitive_deny() -> None:
    _mock_service_token(respx.mock)
    respx.post(_authorize_route()).mock(return_value=httpx.Response(403, json={}))
    auth, tokens = _make_client()
    try:
        with pytest.raises(ApiError) as ei:
            await auth.authorize(
                tenant_id=TEST_TENANT,
                agent_id=TEST_AGENT,
                action="task:execute",
                agent_jwt=TEST_AGENT_JWT,
                valkey=None,  # no cache -> straight read-through
            )
        assert ei.value.code == ErrorCode.FORBIDDEN
    finally:
        await auth.aclose()
        await tokens.aclose()


# ── fail-open: Auth 5xx / transport error accepts ───────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_auth_5xx_fails_open() -> None:
    _mock_service_token(respx.mock)
    respx.post(_authorize_route()).mock(return_value=httpx.Response(503, json={}))
    auth, tokens = _make_client()
    try:
        # No raise — a 5xx is treated as allow (availability wins; JWT already verified).
        await auth.authorize(
            tenant_id=TEST_TENANT,
            agent_id=TEST_AGENT,
            action="task:execute",
            agent_jwt=TEST_AGENT_JWT,
            valkey=None,
        )
    finally:
        await auth.aclose()
        await tokens.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_cache_read_error_falls_through_to_auth() -> None:
    _mock_service_token(respx.mock)
    route = respx.post(_authorize_route()).mock(
        return_value=httpx.Response(200, json={"allowed": True})
    )
    cache = FakeAuthzCache(raise_on_get=RuntimeError("valkey down"))
    auth, tokens = _make_client()
    try:
        await auth.authorize(
            tenant_id=TEST_TENANT,
            agent_id=TEST_AGENT,
            action="task:execute",
            agent_jwt=TEST_AGENT_JWT,
            valkey=cache,
        )
    finally:
        await auth.aclose()
        await tokens.aclose()
    # A cache-read error falls through to Auth (fail-open on the cache), then proceeds.
    assert route.called


@pytest.mark.asyncio
async def test_authorize_disabled_skips_everything() -> None:
    settings = get_settings().model_copy(update={"authorize_enabled": False})
    tokens = ServiceTokenProvider(settings)
    auth = AuthClient(settings, tokens)
    cache = FakeAuthzCache(seed=False)  # would deny if consulted
    try:
        await auth.authorize(  # disabled -> returns without touching cache or Auth
            tenant_id=TEST_TENANT,
            agent_id=TEST_AGENT,
            action="task:execute",
            agent_jwt=TEST_AGENT_JWT,
            valkey=cache,
        )
    finally:
        await auth.aclose()
        await tokens.aclose()
    assert cache.get_calls == 0


# ── HTTP submit path: a definitive deny surfaces as 403 ─────────────────────────────
class _ConfiguredCancelOnlyValkey(FakeAuthzCache):
    """Authorize-cache double that ALSO marks itself configured for _valkey_client."""

    async def set_cancel_signal(self, **_kwargs: Any) -> None:
        return None

    async def clear_cancel_signal(self, **_kwargs: Any) -> None:
        return None

    async def is_cancelled(self, **_kwargs: Any) -> bool:
        return False

    async def aclose(self) -> None:
        return None


async def test_http_submit_denied_returns_403(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    """A cached deny on the verdict cache makes POST /v1/tasks return 403 (no DB needed).

    The deny short-circuits BEFORE create_task, so no task store is required — but the
    pool guard runs first, so we give a dummy handle.
    """
    app = client._transport.app
    app.state.db_pool = object()
    # Configured Valkey whose cache denies -> _authorize_submit raises FORBIDDEN.
    app.state.valkey = _ConfiguredCancelOnlyValkey(seed=False)
    # Build the shared AuthClient over the wired token provider (authorize_enabled default True).

    resp = await client.post(
        "/v1/tasks",
        json={"agent_id": TEST_AGENT, "input": {"message": "hi"}, "mode": "sync"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"
