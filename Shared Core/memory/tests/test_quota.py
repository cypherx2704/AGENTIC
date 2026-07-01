"""Quota + rate enforcement (Contract-19 memory limits), all fail-open."""

from __future__ import annotations

import pytest

from _helpers import bind_principal, make_principal
from memory_service.core.config import Settings
from memory_service.services import quota


def test_resolve_limits_fail_open_no_claim() -> None:
    # No plan claim -> default (free) tier, never raises.
    import asyncio

    limits = asyncio.run(quota.resolve_limits(make_principal(plan=None), pool=None, settings=Settings()))
    assert limits.plan == "free"


def test_resolve_limits_known_plan() -> None:
    import asyncio

    quota.clear_cache()
    limits = asyncio.run(quota.resolve_limits(make_principal(plan="pro"), pool=None, settings=Settings()))
    assert limits.plan == "pro"
    assert limits.memories_max == quota._FALLBACK_LIMITS["pro"].memories_max


def test_enforce_resource_caps_memories_max() -> None:
    from memory_service.core.errors import ApiError

    limits = quota.MemoryLimits("free", memories_max=2, storage_bytes_max=10_000,
                                stores_per_min=60, retrieves_per_min=120)
    with pytest.raises(ApiError) as ei:
        quota.enforce_resource_caps(limits=limits, current_count=2, current_bytes=0,
                                    new_content_bytes=10)
    assert ei.value.code == "QUOTA_EXCEEDED"
    assert ei.value.details["reason"] == "MEMORIES_MAX"


def test_enforce_resource_caps_storage_bytes_max() -> None:
    from memory_service.core.errors import ApiError

    limits = quota.MemoryLimits("free", memories_max=1000, storage_bytes_max=100,
                                stores_per_min=60, retrieves_per_min=120)
    with pytest.raises(ApiError) as ei:
        quota.enforce_resource_caps(limits=limits, current_count=0, current_bytes=95,
                                    new_content_bytes=10)
    assert ei.value.details["reason"] == "STORAGE_BYTES_MAX"


@pytest.mark.asyncio
async def test_stores_per_min_rate_limit_429(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    p = make_principal(plan="free")
    bind_principal(app, p)
    # Force a tiny stores_per_min by injecting a custom limits-resolver via the plan
    # fallback override: shrink the free tier for the test.
    quota.clear_cache()
    orig = quota._FALLBACK_LIMITS["free"]
    quota._FALLBACK_LIMITS["free"] = quota.MemoryLimits("free", 1000, 10_000_000, 2, 120)
    try:
        ok1 = await ac.post("/v1/memories", json={"content": "one"})
        ok2 = await ac.post("/v1/memories", json={"content": "two"})
        over = await ac.post("/v1/memories", json={"content": "three"})
        assert ok1.status_code == 201 and ok2.status_code == 201
        assert over.status_code == 429, over.text
        assert over.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert over.headers.get("Retry-After") is not None
    finally:
        quota._FALLBACK_LIMITS["free"] = orig
        quota.clear_cache()


@pytest.mark.asyncio
async def test_rate_limit_fail_open_without_valkey(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(plan="free"))
    app.state.valkey = None  # no Valkey -> rate check fails open
    quota.clear_cache()
    orig = quota._FALLBACK_LIMITS["free"]
    quota._FALLBACK_LIMITS["free"] = quota.MemoryLimits("free", 1000, 10_000_000, 1, 120)
    try:
        a = await ac.post("/v1/memories", json={"content": "uno"})
        b = await ac.post("/v1/memories", json={"content": "dos"})
        # Both succeed despite stores_per_min=1 because the rate check fails open.
        assert a.status_code == 201 and b.status_code == 201
    finally:
        quota._FALLBACK_LIMITS["free"] = orig
        quota.clear_cache()
