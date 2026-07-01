"""Store + retrieve happy paths (deterministic mock embedder, in-memory repo)."""

from __future__ import annotations

import pytest

from _helpers import bind_principal as _bind_principal
from _helpers import make_principal


@pytest.mark.asyncio
async def test_store_then_search_roundtrip(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    _bind_principal(app, make_principal())

    r = await ac.post(
        "/v1/memories",
        json={"content": "The capital of France is Paris.", "type": "fact", "tags": ["geo"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content"] == "The capital of France is Paris."
    assert body["scope"] == "principal_only"  # default
    assert body["principal_type"] == "agent"
    assert body["deduped"] is False

    s = await ac.post("/v1/memories/search", json={"query": "capital of France", "top_k": 5})
    assert s.status_code == 200, s.text
    sb = s.json()
    assert sb["count"] == 1
    assert sb["results"][0]["content"] == "The capital of France is Paris."
    assert 0.0 <= sb["results"][0]["similarity"] <= 1.0


@pytest.mark.asyncio
async def test_content_over_cap_413(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    _bind_principal(app, make_principal())
    app.state.settings.content_max_bytes = 16  # tiny cap for the test
    r = await ac.post("/v1/memories", json={"content": "x" * 64})
    assert r.status_code == 413, r.text
    assert r.json()["error"]["details"]["reason"] == "CONTENT_TOO_LARGE"


@pytest.mark.asyncio
async def test_search_top_k_capped(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    _bind_principal(app, make_principal())
    app.state.settings.search_top_k_max = 2
    for i in range(5):
        await ac.post("/v1/memories", json={"content": f"memory number {i}"})
    s = await ac.post("/v1/memories/search", json={"query": "memory", "top_k": 50})
    assert s.status_code == 200
    assert s.json()["count"] <= 2


@pytest.mark.asyncio
async def test_type_and_tag_filters(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    _bind_principal(app, make_principal())
    await ac.post("/v1/memories", json={"content": "a fact", "type": "fact", "tags": ["t1"]})
    await ac.post("/v1/memories", json={"content": "a note", "type": "note", "tags": ["t2"]})

    s = await ac.post("/v1/memories/search", json={"query": "a", "type": "fact"})
    res = s.json()["results"]
    assert all(m["type"] == "fact" for m in res)

    s2 = await ac.post("/v1/memories/search", json={"query": "a", "tags": ["t2"]})
    res2 = s2.json()["results"]
    assert all("t2" in m["tags"] for m in res2)
