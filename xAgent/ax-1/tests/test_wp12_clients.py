"""WP12 — downstream client unit tests: McpClient circuit breaker + RegistryClient ETag cache.

Both clients are driven over an INJECTED httpx.AsyncClient + respx so no real socket is
opened, with a fake token provider so the Auth service-token endpoint is never touched.
``time.monotonic`` is monkeypatched in the breaker/cache tests to make cooldown/TTL
transitions deterministic (no sleeps).

McpClient:
  * opens after N CONSECUTIVE failures (5xx/transport), then fast-fails (NO network);
  * half-opens after the cooldown; a trial SUCCESS closes it, a trial FAILURE re-opens;
  * a 4xx is a CLIENT fault: never retried, never trips the breaker (VALIDATION_ERROR);
  * a 5xx is retried up to ``mcp_retry_attempts``.

RegistryClient:
  * MISS -> full GET + backfill; subsequent HIT served from cache with NO network;
  * STALE -> If-None-Match revalidate: 304 reuses body + resets TTL, 200 refreshes;
  * transport/5xx during revalidate serves the cached entry STALE (fail-soft);
  * 404 -> NOT_FOUND; a 4xx is never retried.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.services.mcp_client import McpClient
from agent_runtime.services.registry_client import RegistryClient

AGENT_JWT = "inbound.agent.jwt"
ON_BEHALF = "00000000-0000-0000-0000-0000000000bb"


class _FakeTokens:
    """Stands in for ServiceTokenProvider — never touches the Auth endpoint."""

    async def get_token(self, *, on_behalf_of: str | None = None) -> str:
        return "svc.jwt.token"

    async def aclose(self) -> None:
        return None


def _settings(**overrides: Any) -> Settings:
    base = get_settings().model_dump()
    base.update(overrides)
    return Settings(**base)


class _Clock:
    """A controllable monotonic clock for deterministic cooldown/TTL transitions.

    Installed by replacing the client module's ``time`` reference with a tiny shim that
    forwards everything to the real ``time`` module EXCEPT ``monotonic`` (this clock), so
    only the client's own monotonic reads are virtualised — asyncio/httpx/respx keep the
    real clock and the suite stays fast.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _TimeShim:
    """Delegates to real ``time`` but overrides ``monotonic`` with a controllable clock."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock()

    def __getattr__(self, name: str) -> Any:
        import time as _real_time

        return getattr(_real_time, name)


def _install_clock(monkeypatch: Any, module: Any) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(module, "time", _TimeShim(clock))
    return clock


# ════════════════════════════ McpClient circuit breaker ════════════════════════════
INVOKE_URL = "http://tool-x"
_INVOKE_ENDPOINT = f"{INVOKE_URL}/mcp/v1/invoke"


def _mcp(settings: Settings) -> tuple[McpClient, httpx.AsyncClient]:
    http = httpx.AsyncClient()
    return McpClient(settings, _FakeTokens(), client=http), http


async def _invoke(client: McpClient, *, tool_call_id: str = "tc-1") -> Any:
    return await client.invoke(
        INVOKE_URL, "search", {"q": "x"}, task_id="task-1", tool_call_id=tool_call_id,
        agent_jwt=AGENT_JWT, on_behalf_of=ON_BEHALF,
    )


@respx.mock
async def test_mcp_breaker_opens_after_threshold_then_fast_fails(monkeypatch: Any) -> None:
    import agent_runtime.services.mcp_client as mcp_mod

    _install_clock(monkeypatch, mcp_mod)
    # threshold=2 consecutive failures, retries=0 so each invoke = exactly one attempt.
    settings = _settings(mcp_circuit_breaker_threshold=2, mcp_retry_attempts=0,
                         mcp_circuit_breaker_cooldown_seconds=30.0)
    client, http = _mcp(settings)
    route = respx.post(_INVOKE_ENDPOINT).mock(return_value=httpx.Response(503))
    try:
        # Two 5xx failures cross the threshold and OPEN the breaker.
        for i in range(2):
            with pytest.raises(ApiError) as ei:
                await _invoke(client, tool_call_id=f"tc-{i}")
            assert ei.value.code == ErrorCode.SERVICE_UNAVAILABLE
        assert route.call_count == 2

        # Now OPEN: the next call FAST-FAILS without any network call.
        with pytest.raises(ApiError) as ei:
            await _invoke(client, tool_call_id="tc-open")
        assert ei.value.code == ErrorCode.SERVICE_UNAVAILABLE
        assert "circuit open" in ei.value.message
        assert route.call_count == 2  # unchanged — no network while open
    finally:
        await http.aclose()


@respx.mock
async def test_mcp_breaker_half_open_success_closes(monkeypatch: Any) -> None:
    import agent_runtime.services.mcp_client as mcp_mod

    clock = _install_clock(monkeypatch, mcp_mod)
    settings = _settings(mcp_circuit_breaker_threshold=1, mcp_retry_attempts=0,
                         mcp_circuit_breaker_cooldown_seconds=10.0)
    client, http = _mcp(settings)
    route = respx.post(_INVOKE_ENDPOINT)
    try:
        # 1 failure opens the breaker (threshold=1).
        route.mock(return_value=httpx.Response(500))
        with pytest.raises(ApiError):
            await _invoke(client, tool_call_id="f1")
        # Still within cooldown -> fast-fail, no network.
        with pytest.raises(ApiError) as ei:
            await _invoke(client, tool_call_id="f2")
        assert "circuit open" in ei.value.message

        # Advance past cooldown -> HALF-OPEN: one trial call is allowed and SUCCEEDS -> CLOSE.
        clock.advance(11.0)
        route.mock(return_value=httpx.Response(200, json={"tool": "search", "result": {"ok": 1}}))
        result = await _invoke(client, tool_call_id="trial")
        assert result.result == {"ok": 1}

        # Breaker CLOSED again: a subsequent call flows normally.
        result2 = await _invoke(client, tool_call_id="after")
        assert result2.result == {"ok": 1}
    finally:
        await http.aclose()


@respx.mock
async def test_mcp_breaker_half_open_failure_reopens(monkeypatch: Any) -> None:
    import agent_runtime.services.mcp_client as mcp_mod

    clock = _install_clock(monkeypatch, mcp_mod)
    settings = _settings(mcp_circuit_breaker_threshold=1, mcp_retry_attempts=0,
                         mcp_circuit_breaker_cooldown_seconds=10.0)
    client, http = _mcp(settings)
    route = respx.post(_INVOKE_ENDPOINT).mock(return_value=httpx.Response(500))
    try:
        with pytest.raises(ApiError):  # opens
            await _invoke(client, tool_call_id="f1")
        clock.advance(11.0)  # half-open window
        calls_before = route.call_count
        with pytest.raises(ApiError):  # half-open TRIAL fails -> RE-OPEN
            await _invoke(client, tool_call_id="trial")
        assert route.call_count == calls_before + 1  # the trial DID hit the network

        # Re-opened: immediately fast-fails again (cooldown restarted).
        with pytest.raises(ApiError) as ei:
            await _invoke(client, tool_call_id="after")
        assert "circuit open" in ei.value.message
        assert route.call_count == calls_before + 1  # no further network
    finally:
        await http.aclose()


@respx.mock
async def test_mcp_4xx_is_terminal_never_retried_never_trips_breaker(monkeypatch: Any) -> None:
    import agent_runtime.services.mcp_client as mcp_mod

    _install_clock(monkeypatch, mcp_mod)
    settings = _settings(mcp_circuit_breaker_threshold=1, mcp_retry_attempts=3,
                         mcp_circuit_breaker_cooldown_seconds=10.0)
    client, http = _mcp(settings)
    route = respx.post(_INVOKE_ENDPOINT).mock(return_value=httpx.Response(400, json={"e": "bad"}))
    try:
        # A 4xx -> VALIDATION_ERROR on the FIRST response (never retried).
        with pytest.raises(ApiError) as ei:
            await _invoke(client, tool_call_id="bad-1")
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
        assert ei.value.details["status"] == 400
        assert route.call_count == 1  # NOT retried despite retries=3

        # The 4xx did NOT trip the breaker (threshold=1) — a subsequent call still flows.
        route.mock(return_value=httpx.Response(200, json={"tool": "search", "result": "ok"}))
        result = await _invoke(client, tool_call_id="good")
        assert result.result == "ok"
    finally:
        await http.aclose()


@respx.mock
async def test_mcp_5xx_retried_up_to_attempts(monkeypatch: Any) -> None:
    import agent_runtime.services.mcp_client as mcp_mod

    _install_clock(monkeypatch, mcp_mod)
    # retries=2 -> 3 total attempts; high threshold so the breaker does not interfere.
    settings = _settings(mcp_retry_attempts=2, mcp_circuit_breaker_threshold=10)
    client, http = _mcp(settings)
    # First two attempts 5xx, third succeeds.
    responses = [httpx.Response(500), httpx.Response(500), httpx.Response(200, json={"result": "ok"})]
    route = respx.post(_INVOKE_ENDPOINT).mock(side_effect=responses)
    try:
        result = await _invoke(client, tool_call_id="retry")
        assert result.result == "ok"
        assert route.call_count == 3  # initial + 2 retries
        # Idempotency-Key is the SAME (task:tool_call_id) on every retry.
        keys = {call.request.headers.get("Idempotency-Key") for call in route.calls}
        assert keys == {"task-1:retry"}
    finally:
        await http.aclose()


# ════════════════════════════ RegistryClient ETag cache ════════════════════════════
TOOL_NAME = "search"


def _registry(settings: Settings) -> tuple[RegistryClient, httpx.AsyncClient]:
    http = httpx.AsyncClient()
    return RegistryClient(settings, _FakeTokens(), client=http), http


def _tool_endpoint(settings: Settings) -> str:
    return f"{settings.tool_registry_url.rstrip('/')}/v1/tools/{TOOL_NAME}"


def _tool_body(version: str = "1.0.0") -> dict[str, Any]:
    return {"name": TOOL_NAME, "version": version, "manifest": {"description": "d"},
            "invoke_url": "http://tool", "required_scopes": []}


async def _resolve(client: RegistryClient) -> Any:
    return await client.resolve_tool(TOOL_NAME, agent_jwt=AGENT_JWT, on_behalf_of=ON_BEHALF)


@respx.mock
async def test_registry_miss_then_hit_no_network(monkeypatch: Any) -> None:
    import agent_runtime.services.registry_client as reg_mod

    _install_clock(monkeypatch, reg_mod)
    settings = _settings(registry_manifest_cache_ttl_seconds=300)
    client, http = _registry(settings)
    route = respx.get(_tool_endpoint(settings)).mock(
        return_value=httpx.Response(200, headers={"ETag": "v1"}, json=_tool_body("1.0.0"))
    )
    try:
        # MISS -> full GET + backfill.
        first = await _resolve(client)
        assert first.version == "1.0.0"
        assert route.call_count == 1

        # Still FRESH (TTL 300, clock unchanged) -> served from cache, NO network.
        second = await _resolve(client)
        assert second.version == "1.0.0"
        assert route.call_count == 1  # unchanged — a cache hit
    finally:
        await http.aclose()


@respx.mock
async def test_registry_stale_304_revalidates_and_resets_ttl(monkeypatch: Any) -> None:
    import agent_runtime.services.registry_client as reg_mod

    clock = _install_clock(monkeypatch, reg_mod)
    settings = _settings(registry_manifest_cache_ttl_seconds=10)
    client, http = _registry(settings)
    route = respx.get(_tool_endpoint(settings))
    try:
        route.mock(return_value=httpx.Response(200, headers={"ETag": "v1"}, json=_tool_body("1.0.0")))
        await _resolve(client)  # MISS, caches with ETag v1
        assert route.call_count == 1

        # Expire the TTL -> revalidate with If-None-Match; the registry says 304 Not Modified.
        clock.advance(11.0)
        route.mock(return_value=httpx.Response(304))
        revalidated = await _resolve(client)
        assert revalidated.version == "1.0.0"  # reused cached body
        assert route.call_count == 2
        # The revalidation sent If-None-Match: v1.
        assert route.calls[-1].request.headers.get("If-None-Match") == "v1"

        # TTL reset by the 304 -> the next call is a fresh cache hit (NO network).
        served = await _resolve(client)
        assert served.version == "1.0.0"
        assert route.call_count == 2
    finally:
        await http.aclose()


@respx.mock
async def test_registry_stale_200_refreshes_body(monkeypatch: Any) -> None:
    import agent_runtime.services.registry_client as reg_mod

    clock = _install_clock(monkeypatch, reg_mod)
    settings = _settings(registry_manifest_cache_ttl_seconds=10)
    client, http = _registry(settings)
    route = respx.get(_tool_endpoint(settings))
    try:
        route.mock(return_value=httpx.Response(200, headers={"ETag": "v1"}, json=_tool_body("1.0.0")))
        await _resolve(client)

        clock.advance(11.0)  # stale
        # 200 with a NEW body + ETag -> refresh the cache.
        route.mock(return_value=httpx.Response(200, headers={"ETag": "v2"}, json=_tool_body("2.0.0")))
        refreshed = await _resolve(client)
        assert refreshed.version == "2.0.0"
        assert route.call_count == 2

        # Subsequent fresh hit serves the REFRESHED (2.0.0) body with no network.
        served = await _resolve(client)
        assert served.version == "2.0.0"
        assert route.call_count == 2
    finally:
        await http.aclose()


@respx.mock
async def test_registry_5xx_during_revalidate_serves_stale(monkeypatch: Any) -> None:
    import agent_runtime.services.registry_client as reg_mod

    clock = _install_clock(monkeypatch, reg_mod)
    # retries=0 so a single 5xx exhausts retries and we fall back to the cache.
    settings = _settings(registry_manifest_cache_ttl_seconds=10, registry_retry_attempts=0)
    client, http = _registry(settings)
    route = respx.get(_tool_endpoint(settings))
    try:
        route.mock(return_value=httpx.Response(200, headers={"ETag": "v1"}, json=_tool_body("1.0.0")))
        await _resolve(client)  # cache it

        clock.advance(11.0)  # stale
        route.mock(return_value=httpx.Response(503))  # registry blip during revalidate
        stale = await _resolve(client)
        assert stale.version == "1.0.0"  # served the STALE cached entry (fail-soft)
    finally:
        await http.aclose()


@respx.mock
async def test_registry_transport_error_during_revalidate_serves_stale(monkeypatch: Any) -> None:
    import agent_runtime.services.registry_client as reg_mod

    clock = _install_clock(monkeypatch, reg_mod)
    settings = _settings(registry_manifest_cache_ttl_seconds=10, registry_retry_attempts=0)
    client, http = _registry(settings)
    route = respx.get(_tool_endpoint(settings))
    try:
        route.mock(return_value=httpx.Response(200, headers={"ETag": "v1"}, json=_tool_body("1.0.0")))
        await _resolve(client)

        clock.advance(11.0)
        route.mock(side_effect=httpx.ConnectError("boom"))
        stale = await _resolve(client)
        assert stale.version == "1.0.0"  # transport blip -> stale cache served
    finally:
        await http.aclose()


@respx.mock
async def test_registry_404_is_not_found(monkeypatch: Any) -> None:
    settings = _settings()
    client, http = _registry(settings)
    respx.get(_tool_endpoint(settings)).mock(return_value=httpx.Response(404))
    try:
        with pytest.raises(ApiError) as ei:
            await _resolve(client)
        assert ei.value.code == ErrorCode.NOT_FOUND
    finally:
        await http.aclose()


@respx.mock
async def test_registry_no_cache_transport_error_raises(monkeypatch: Any) -> None:
    # retries=0, no cached entry -> a transport error raises SERVICE_UNAVAILABLE (no stale fallback).
    settings = _settings(registry_retry_attempts=0)
    client, http = _registry(settings)
    respx.get(_tool_endpoint(settings)).mock(side_effect=httpx.ConnectError("down"))
    try:
        with pytest.raises(ApiError) as ei:
            await _resolve(client)
        assert ei.value.code == ErrorCode.SERVICE_UNAVAILABLE
    finally:
        await http.aclose()
