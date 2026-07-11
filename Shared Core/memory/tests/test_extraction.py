"""B5 — salient-fact extraction at ingest: the split logic, the API fan-out (aggregate 201,
per-fact dedup, idempotency replay), single-fact/flag-off byte-identical, and fail-soft.
"""

from __future__ import annotations

import pytest

from _helpers import bind_principal, make_principal
from memory_service.services import extraction
from memory_service.services.extraction import extract_facts


def test_extract_facts_splits_multi_fact_content() -> None:
    facts = extract_facts(
        "user favorite color is teal. user lives in berlin. user works as an engineer."
    )
    assert len(facts) == 3
    assert any("teal" in f for f in facts)
    assert any("berlin" in f for f in facts)


def test_extract_facts_single_fact_returns_original() -> None:
    # Nothing to fan out => the original content, unchanged (drives the byte-identical path).
    assert extract_facts("remember the milk") == ["remember the milk"]
    assert extract_facts("") == [""]


def test_extract_facts_splits_conjunctions_when_both_sides_stand_alone() -> None:
    facts = extract_facts("i like green tea and i strongly dislike black coffee")
    assert len(facts) == 2


def test_extract_facts_respects_max_facts() -> None:
    src = ". ".join(f"fact number {i} is noted" for i in range(20))
    assert len(extract_facts(src, max_facts=5)) == 5


@pytest.mark.asyncio
async def test_extract_facts_llm_seam_returns_none_skeleton() -> None:
    assert await extraction.extract_facts_llm(object(), "anything") is None


@pytest.mark.asyncio
async def test_flag_off_stores_single_flat_record(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    r = await ac.post(
        "/v1/memories",
        json={"content": "fact one here. fact two here. fact three here."},
    )
    assert r.status_code == 201
    body = r.json()
    assert "extracted" not in body  # default off => today's flat single-record shape
    assert body["content"].startswith("fact one")


@pytest.mark.asyncio
async def test_fan_out_stores_each_fact_as_its_own_row(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.settings.memory_extraction_enabled = True
    r = await ac.post(
        "/v1/memories",
        json={"content": "user likes teal. user lives in berlin. user codes in python."},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["extracted"] is True
    assert body["count"] == 3
    assert len(body["memories"]) == 3
    contents = [m["content"] for m in body["memories"]]
    assert any("teal" in c for c in contents)
    # Three distinct rows now exist for the principal.
    count, _ = await app.state.repo.resource_usage(
        make_principal().tenant_id, "agent", make_principal().agent_id
    )
    assert count == 3


@pytest.mark.asyncio
async def test_single_fact_content_stays_flat_even_when_enabled(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.settings.memory_extraction_enabled = True
    r = await ac.post("/v1/memories", json={"content": "remember the milk"})
    assert r.status_code == 201
    assert "extracted" not in r.json()  # single fact => no fan-out, byte-identical shape


@pytest.mark.asyncio
async def test_fan_out_idempotency_replays_aggregate_body(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.settings.memory_extraction_enabled = True
    spy = app.state.embedder
    h = {"Idempotency-Key": "extract-1"}
    payload = {"content": "alpha fact here. beta fact here. gamma fact here."}
    first = await ac.post("/v1/memories", headers=h, json=payload)
    assert first.status_code == 201 and first.json()["count"] == 3
    calls = spy.embed_calls
    second = await ac.post("/v1/memories", headers=h, json=payload)
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json() == first.json()   # aggregate replay, verbatim
    assert spy.embed_calls == calls        # replay does not re-embed any fact


@pytest.mark.asyncio
async def test_extractor_error_fails_soft_to_raw_content(app_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.settings.memory_extraction_enabled = True

    def _boom(content, *, max_facts=16):  # type: ignore[no-untyped-def]
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr("memory_service.api.memories.extraction.extract_facts", _boom)
    r = await ac.post(
        "/v1/memories", json={"content": "fact one here. fact two here. fact three here."}
    )
    assert r.status_code == 201
    body = r.json()
    assert "extracted" not in body                     # fail-soft => single raw row
    assert body["content"].startswith("fact one")
