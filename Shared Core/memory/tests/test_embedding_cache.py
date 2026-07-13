"""B2 — content-hash embedding cache (Valkey): hit avoids re-embed, fail-open, flag-off,
model/dim namespacing, and an app-level "no re-embed on repeated identical text" check
mirroring tests/test_dedup_and_idempotency.py.
"""

from __future__ import annotations

import pytest

from _helpers import FakeValkey, SpyEmbeddingClient, bind_principal, make_principal
from memory_service.core.config import Settings
from memory_service.services.embeddings import EmbeddingClient


def _cache_settings(**kw) -> Settings:  # type: ignore[no-untyped-def]
    kw.setdefault("embeddings_vector_dim", 64)
    return Settings(
        embeddings_mock_fallback=True, memory_embedding_cache_enabled=True, **kw,
    )


@pytest.mark.asyncio
async def test_hit_serves_cached_vector_without_reembedding() -> None:
    spy = SpyEmbeddingClient(_cache_settings(), valkey=FakeValkey())
    v1, s1 = await spy.embed_one("hello world")
    assert spy.embed_calls == 1 and s1 == "mock"
    v2, s2 = await spy.embed_one("hello world")
    assert spy.embed_calls == 1  # THE assertion: served from cache, no new embed
    assert s2 == "cache"
    assert v1 == v2


@pytest.mark.asyncio
async def test_batch_partial_hit_embeds_only_misses_in_input_order() -> None:
    spy = SpyEmbeddingClient(_cache_settings(), valkey=FakeValkey())
    await spy.embed_many(["a"])          # cache "a"
    base = spy.embed_calls
    vecs, _src = await spy.embed_many(["a", "b", "a"])  # only "b" is a miss
    assert spy.embed_calls == base + 1
    assert len(vecs) == 3
    assert vecs[0] == vecs[2]            # both "a", re-merged in input order


@pytest.mark.asyncio
async def test_flag_off_never_caches_byte_identical_to_today() -> None:
    spy = SpyEmbeddingClient(
        Settings(embeddings_mock_fallback=True, embeddings_vector_dim=64,
                 memory_embedding_cache_enabled=False),
        valkey=FakeValkey(),
    )
    await spy.embed_one("x")
    await spy.embed_one("x")
    assert spy.embed_calls == 2  # no cache => embeds every time (today's path)


@pytest.mark.asyncio
async def test_valkey_error_fails_open_to_normal_embed() -> None:
    class BoomValkey:
        async def get(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("valkey down")

        async def set(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("valkey down")

    spy = SpyEmbeddingClient(_cache_settings(), valkey=BoomValkey())
    v1, _ = await spy.embed_one("x")          # GET raises -> miss -> embed; SET raises -> ignored
    assert spy.embed_calls == 1 and len(v1) == 64
    await spy.embed_one("x")                   # GET raises again -> miss -> embed again
    assert spy.embed_calls == 2                # fail-open: never serves a stale/blocked vector


@pytest.mark.asyncio
async def test_dim_change_never_serves_a_stale_vector() -> None:
    valkey = FakeValkey()
    c64 = EmbeddingClient(_cache_settings(embeddings_vector_dim=64), valkey=valkey)
    c128 = EmbeddingClient(_cache_settings(embeddings_vector_dim=128), valkey=valkey)
    v64, _ = await c64.embed_one("same text")
    v128, s128 = await c128.embed_one("same text")
    assert len(v64) == 64 and len(v128) == 128  # dim in the key => different namespace
    assert s128 == "mock"                        # a real embed, not the 64-dim cache entry


@pytest.mark.asyncio
async def test_model_change_uses_a_different_key() -> None:
    valkey = FakeValkey()
    a = EmbeddingClient(_cache_settings(embeddings_model="embed"), valkey=valkey)
    b = EmbeddingClient(_cache_settings(embeddings_model="embed-v2"), valkey=valkey)
    assert a._cache_key("hi") != b._cache_key("hi")  # noqa: SLF001 — key-namespace check


@pytest.mark.asyncio
async def test_app_repeated_identical_store_and_search_embeds_once(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    # Turn the cache on and give the embedder Valkey (the fixture builds it cache-less).
    app.state.settings.memory_embedding_cache_enabled = True
    spy = SpyEmbeddingClient(app.state.settings, valkey=app.state.valkey)
    app.state.embedder = spy

    r1 = await ac.post("/v1/memories", json={"content": "cache me once"})
    assert r1.status_code == 201
    assert spy.embed_calls == 1
    # Re-store identical content: the embed is served from cache (dedup then bumps).
    r2 = await ac.post("/v1/memories", json={"content": "cache me once"})
    assert r2.status_code == 201
    assert spy.embed_calls == 1  # NO re-embed on repeated identical text
    # Search the same text: query embed is a cache hit too.
    s = await ac.post("/v1/memories/search", json={"query": "cache me once"})
    assert s.status_code == 200
    assert spy.embed_calls == 1
