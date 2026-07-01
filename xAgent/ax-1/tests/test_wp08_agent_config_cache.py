"""WP08 — agent-config Valkey read-through cache (services/agent_config_cache.py).

The cache talks to the underlying redis-asyncio client via ``valkey.client()`` and the
client's ``get`` / ``set`` / ``delete``. We build a fake ``ValkeyClient`` exposing a
``client()`` that returns a tiny in-memory redis double, and stub
``agents_repo.get_agent`` so no DB is touched.

Covered:
  * read-through MISS -> DB read + backfill into the cache;
  * read-through HIT  -> served from cache, NO DB read;
  * invalidate()      -> deletes the cache key;
  * fail-open when valkey is absent (None) or lacks ``client()`` (conftest fake) -> DB read;
  * a cross-tenant cached blob is treated as a miss (never served to the wrong tenant);
  * a redis error on get -> fail-open to a DB read.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.core.config import get_settings
from agent_runtime.db import agents_repo
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.services import agent_config_cache

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000cc"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


# ── In-memory redis + a ValkeyClient-shaped wrapper exposing client() ───────────────
class _FakeRedis:
    def __init__(self, *, raise_on_get: Exception | None = None) -> None:
        self.store: dict[str, str] = {}
        self.raise_on_get = raise_on_get
        self.get_calls = 0
        self.set_calls = 0
        self.delete_calls = 0

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.set_calls += 1
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.delete_calls += 1
        self.store.pop(key, None)


class _FakeValkeyWithClient:
    """ValkeyClient-shaped: exposes client() so agent_config_cache uses it."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis

    def client(self) -> _FakeRedis:
        return self._redis


def _runtime(tenant_id: str = TEST_TENANT) -> AgentRuntime:
    return AgentRuntime(
        agent_id=TEST_AGENT,
        tenant_id=tenant_id,
        name="Test Agent",
        system_prompt="You are helpful.",
    )


def _patch_db(monkeypatch: Any, runtime: AgentRuntime | None, counter: dict[str, int]) -> None:
    async def _get_agent(pool: Any, tenant_id: str, agent_id: str) -> AgentRuntime | None:
        counter["db_reads"] = counter.get("db_reads", 0) + 1
        return runtime

    monkeypatch.setattr(agents_repo, "get_agent", _get_agent)


# ── miss -> DB read + backfill ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_read_through_miss_reads_db_and_backfills(monkeypatch: Any) -> None:
    counter: dict[str, int] = {}
    _patch_db(monkeypatch, _runtime(), counter)
    redis = _FakeRedis()
    valkey = _FakeValkeyWithClient(redis)
    settings = get_settings()

    result = await agent_config_cache.get_runtime(
        valkey, object(), settings, TEST_TENANT, TEST_AGENT
    )

    assert result is not None and result.agent_id == TEST_AGENT
    assert counter["db_reads"] == 1  # cache miss -> one DB read
    # The resolved config was backfilled into the cache for the next call.
    key = f"{settings.agent_config_cache_key_prefix}{TEST_AGENT}"
    assert key in redis.store
    assert redis.set_calls == 1


# ── hit -> served from cache, NO DB read ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_read_through_hit_skips_db(monkeypatch: Any) -> None:
    counter: dict[str, int] = {}
    _patch_db(monkeypatch, _runtime(), counter)
    redis = _FakeRedis()
    settings = get_settings()
    # Pre-seed the cache with a valid same-tenant blob.
    key = f"{settings.agent_config_cache_key_prefix}{TEST_AGENT}"
    redis.store[key] = _runtime().model_dump_json()
    valkey = _FakeValkeyWithClient(redis)

    result = await agent_config_cache.get_runtime(
        valkey, object(), settings, TEST_TENANT, TEST_AGENT
    )

    assert result is not None and result.agent_id == TEST_AGENT
    assert counter.get("db_reads", 0) == 0  # HIT -> no DB read


# ── cross-tenant cached blob is NOT served (treated as a miss) ──────────────────────
@pytest.mark.asyncio
async def test_cross_tenant_cache_blob_is_miss(monkeypatch: Any) -> None:
    counter: dict[str, int] = {}
    _patch_db(monkeypatch, _runtime(TEST_TENANT), counter)
    redis = _FakeRedis()
    settings = get_settings()
    key = f"{settings.agent_config_cache_key_prefix}{TEST_AGENT}"
    # Cache holds ANOTHER tenant's config under this agent key (key collision).
    redis.store[key] = _runtime(OTHER_TENANT).model_dump_json()
    valkey = _FakeValkeyWithClient(redis)

    result = await agent_config_cache.get_runtime(
        valkey, object(), settings, TEST_TENANT, TEST_AGENT
    )

    # The mismatched blob must not be served -> re-read under RLS from the DB.
    assert result is not None and result.tenant_id == TEST_TENANT
    assert counter["db_reads"] == 1


# ── invalidate() deletes the key ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_invalidate_deletes_key() -> None:
    redis = _FakeRedis()
    settings = get_settings()
    key = f"{settings.agent_config_cache_key_prefix}{TEST_AGENT}"
    redis.store[key] = "{}"
    valkey = _FakeValkeyWithClient(redis)

    await agent_config_cache.invalidate(valkey, settings, TEST_AGENT)

    assert key not in redis.store
    assert redis.delete_calls == 1


# ── fail-open: no valkey -> straight DB read (bypass) ───────────────────────────────
@pytest.mark.asyncio
async def test_no_valkey_bypasses_to_db(monkeypatch: Any) -> None:
    counter: dict[str, int] = {}
    _patch_db(monkeypatch, _runtime(), counter)
    settings = get_settings()

    result = await agent_config_cache.get_runtime(None, object(), settings, TEST_TENANT, TEST_AGENT)

    assert result is not None
    assert counter["db_reads"] == 1  # no cache -> direct DB read


# ── fail-open: conftest-style fake without client() -> bypass ───────────────────────
@pytest.mark.asyncio
async def test_fake_without_client_bypasses(monkeypatch: Any) -> None:
    counter: dict[str, int] = {}
    _patch_db(monkeypatch, _runtime(), counter)
    settings = get_settings()

    class _NoClientFake:  # mirrors the conftest network-free double (no client())
        async def ping(self) -> bool:
            return False

    result = await agent_config_cache.get_runtime(
        _NoClientFake(), object(), settings, TEST_TENANT, TEST_AGENT
    )

    assert result is not None
    assert counter["db_reads"] == 1


# ── fail-open: a redis error on get -> DB read ──────────────────────────────────────
@pytest.mark.asyncio
async def test_redis_get_error_fails_open(monkeypatch: Any) -> None:
    counter: dict[str, int] = {}
    _patch_db(monkeypatch, _runtime(), counter)
    redis = _FakeRedis(raise_on_get=RuntimeError("valkey down"))
    valkey = _FakeValkeyWithClient(redis)
    settings = get_settings()

    result = await agent_config_cache.get_runtime(
        valkey, object(), settings, TEST_TENANT, TEST_AGENT
    )

    assert result is not None
    assert counter["db_reads"] == 1  # error -> fail open to DB


# ── disabled flag -> bypass even with a client() present ────────────────────────────
@pytest.mark.asyncio
async def test_cache_disabled_bypasses(monkeypatch: Any) -> None:
    counter: dict[str, int] = {}
    _patch_db(monkeypatch, _runtime(), counter)
    redis = _FakeRedis()
    valkey = _FakeValkeyWithClient(redis)
    settings = get_settings().model_copy(update={"agent_config_cache_enabled": False})

    result = await agent_config_cache.get_runtime(
        valkey, object(), settings, TEST_TENANT, TEST_AGENT
    )

    assert result is not None
    assert counter["db_reads"] == 1
    assert redis.get_calls == 0  # cache never consulted when disabled
