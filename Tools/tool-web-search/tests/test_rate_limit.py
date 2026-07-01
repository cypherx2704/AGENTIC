"""POST /mcp/v1/invoke — per-tenant fixed-window rate limiting (fail-open).

Deterministic crossing of the limit without issuing 60+ calls: pre-seed the current
fixed-window request counter (computing the same window id the limiter uses) so a SINGLE
request crosses the threshold -> 429 + Retry-After. Also covers the no-Valkey and
Valkey-down fail-open paths (must still 200).
"""

from __future__ import annotations

import time

import pytest

from tool_web_search.core.config import get_settings

from .conftest import TEST_TENANT, DownValkey, FakeValkey

_INVOKE = "/mcp/v1/invoke"
_ARGS = {"args": {"query": "rate-limit"}}


def _req_key() -> str:
    s = get_settings()
    window = int(time.time()) // s.rate_limit_window_seconds
    return f"{s.rate_limit_key_prefix}req:{TEST_TENANT}:{window}"


@pytest.mark.asyncio
async def test_over_limit_429_with_retry_after(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    # Seed the request counter AT the limit; the next INCR -> limit+1 -> reject.
    valkey.store[_req_key()] = str(get_settings().rate_limit_requests_per_min)
    ac = await make_client(valkey=valkey)

    resp = await ac.post(_INVOKE, json=_ARGS)
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["details"]["dimension"] == "requests"
    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) >= 1


@pytest.mark.asyncio
async def test_under_limit_allows_and_increments(make_client) -> None:  # type: ignore[no-untyped-def]
    valkey = FakeValkey()
    ac = await make_client(valkey=valkey)
    resp = await ac.post(_INVOKE, json=_ARGS)
    assert resp.status_code == 200, resp.text
    assert int(valkey.store[_req_key()]) == 1


@pytest.mark.asyncio
async def test_no_valkey_fail_open_allows(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client(valkey=None)
    resp = await ac.post(_INVOKE, json=_ARGS)
    assert resp.status_code == 200, resp.text  # no Valkey -> never 429


@pytest.mark.asyncio
async def test_valkey_down_fail_open_allows(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client(valkey=DownValkey())
    resp = await ac.post(_INVOKE, json=_ARGS)
    assert resp.status_code == 200, resp.text  # Valkey error -> fail open
