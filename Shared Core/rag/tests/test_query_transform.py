"""Query-transformation features B2 (decomposition) + B3 (multi-query expansion / RAG-Fusion).

Same in-process style as ``test_hybrid_rerank.py`` — service clients exercised directly against
mock + httpx.MockTransport, then the full API path via the ASGI fixtures. Asserts the DEFAULT
(flag off) invariants (both are no-ops, results byte-identical to the un-enhanced path), the
enabled behaviour, and fail-soft degradation to single-query on a gateway outage (never fabricated).
"""

from __future__ import annotations

import httpx
import pytest

from rag_service.core.config import Settings
from rag_service.services.decompose import QueryDecomposer, mock_decompose
from rag_service.services.fusion import reciprocal_rank_fusion
from rag_service.services.multiquery import QueryExpander, mock_expand

AUTH = {"Authorization": "Bearer test"}


class _FakeTokens:
    async def get_token(self, *, on_behalf_of=None):  # noqa: ANN001
        return "svc.jwt"


# ── B3: application-level RRF fusion ────────────────────────────────────────────────
def test_rrf_fusion_rewards_items_ranked_high_across_lists() -> None:
    # 'a' is #1 in both lists; 'b' is #2 then #1-absent; RRF ranks 'a' first, deterministic ties.
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["a", "c", "d"]], k=60)
    order = [cid for cid, _ in fused]
    assert order[0] == "a"
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)
    # 'a' appears at rank 1 in both lists → 2/(60+1); others get one contribution only.
    assert dict(fused)["a"] == pytest.approx(2.0 / 61.0)


def test_rrf_fusion_empty_lists() -> None:
    assert reciprocal_rank_fusion([], k=60) == []
    assert reciprocal_rank_fusion([[], []], k=60) == []


# ── B2: QueryDecomposer ─────────────────────────────────────────────────────────────
def test_mock_decompose_splits_compound_query() -> None:
    subs = mock_decompose("What is the refund window and how do I request a refund?", 4)
    assert len(subs) == 2
    assert "refund window" in subs[0].lower()


def test_mock_decompose_atomic_query_stays_single() -> None:
    assert mock_decompose("reset my password", 4) == ["reset my password"]


def test_mock_decompose_respects_cap() -> None:
    q = "what is A and what is B and what is C and what is D and what is E"
    assert len(mock_decompose(q, 2)) == 2


@pytest.mark.asyncio
async def test_decompose_mock_mode() -> None:
    dec = QueryDecomposer(Settings(mock_embeddings=True, rag_decompose_enabled=True))
    subs, source = await dec.decompose("refund policy and shipping cost")
    assert source == "mock"
    assert len(subs) == 2


@pytest.mark.asyncio
async def test_decompose_via_llms_parses_lines() -> None:
    settings = Settings(mock_embeddings=False, rag_decompose_enabled=True)

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "1. what is the refund window\n2. how to request a refund"}}]
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(_ok))
    dec = QueryDecomposer(settings, token_provider=_FakeTokens(), client=http)
    subs, source = await dec.decompose("compound q", agent_jwt="a.jwt")
    assert source == "llms"
    assert subs == ["what is the refund window", "how to request a refund"]  # numbering stripped


@pytest.mark.asyncio
async def test_decompose_fails_soft_to_single_on_gateway_error() -> None:
    settings = Settings(mock_embeddings=False, rag_decompose_enabled=True)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway down")

    http = httpx.AsyncClient(transport=httpx.MockTransport(_boom))
    dec = QueryDecomposer(settings, token_provider=_FakeTokens(), client=http)
    subs, source = await dec.decompose("compound q and other q", agent_jwt="a.jwt")
    assert source == "fallback_single"
    assert subs == ["compound q and other q"]  # NEVER fabricated — the original query verbatim


# ── B3: QueryExpander ───────────────────────────────────────────────────────────────
def test_mock_expand_keeps_original_first() -> None:
    variants = mock_expand("refund policy", 3)
    assert variants[0] == "refund policy"
    assert len(variants) == 4  # original + 3 variants
    assert len(set(variants)) == len(variants)  # de-duplicated


@pytest.mark.asyncio
async def test_multiquery_mock_mode() -> None:
    exp = QueryExpander(Settings(mock_embeddings=True, rag_multiquery_enabled=True))
    variants, source = await exp.expand("refund policy", n=2)
    assert source == "mock"
    assert variants[0] == "refund policy"
    assert len(variants) == 3


@pytest.mark.asyncio
async def test_multiquery_via_llms_parses_and_prepends_original() -> None:
    settings = Settings(mock_embeddings=False, rag_multiquery_enabled=True)

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "- reimbursement policy\n- money return rules"}}]
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(_ok))
    exp = QueryExpander(settings, token_provider=_FakeTokens(), client=http)
    variants, source = await exp.expand("get money back", n=5, agent_jwt="a.jwt")
    assert source == "llms"
    assert variants[0] == "get money back"
    assert "reimbursement policy" in variants


@pytest.mark.asyncio
async def test_multiquery_fails_soft_to_single_on_gateway_error() -> None:
    settings = Settings(mock_embeddings=False, rag_multiquery_enabled=True)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway down")

    http = httpx.AsyncClient(transport=httpx.MockTransport(_boom))
    exp = QueryExpander(settings, token_provider=_FakeTokens(), client=http)
    variants, source = await exp.expand("refund policy", n=3, agent_jwt="a.jwt")
    assert source == "fallback_single"
    assert variants == ["refund policy"]  # NEVER fabricated


# ── API integration ─────────────────────────────────────────────────────────────────
async def _create_kb(client, name: str) -> dict:  # noqa: ANN001
    resp = await client.post("/v1/kbs", json={"name": name}, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _ingest(client, kb_id: str, name: str, content: str) -> None:  # noqa: ANN001
    resp = await client.post(
        f"/v1/kbs/{kb_id}/documents",
        json={"name": name, "content": content, "source_type": "markdown"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text


async def _seed_kb(client, name: str) -> str:  # noqa: ANN001
    kb = await _create_kb(client, name)
    await _ingest(client, kb["kb_id"], "a.md",
                  "The refund policy allows a full refund within 30 days for enterprise plans.")
    await _ingest(client, kb["kb_id"], "b.md",
                  "Shipping is free for orders over fifty dollars worldwide.")
    return kb["kb_id"]


@pytest.mark.asyncio
async def test_query_decompose_flag_off_is_noop(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    # RAG_DECOMPOSE_ENABLED off ⇒ decompose=true is a no-op (usage 'decomposed' stays false).
    kb_id = await _seed_kb(app_client, "nodecompkb")
    resp = await app_client.post(
        f"/v1/kbs/{kb_id}/query",
        json={"query": "refund policy and shipping cost", "top_k": 3, "decompose": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    usage = fake_db.outbox_payloads("cypherx.rag.usage.recorded")[-1]
    assert usage["units"]["decomposed"] is False


@pytest.mark.asyncio
async def test_query_decompose_flag_off_byte_identical(app_client, auth_as) -> None:  # noqa: ANN001
    # With the flag off, sending decompose=true returns EXACTLY the default-path results.
    kb_id = await _seed_kb(app_client, "identkb")
    base = await app_client.post(
        f"/v1/kbs/{kb_id}/query", json={"query": "refund policy", "top_k": 3}, headers=AUTH)
    withflag = await app_client.post(
        f"/v1/kbs/{kb_id}/query",
        json={"query": "refund policy", "top_k": 3, "decompose": True, "multi_query": True},
        headers=AUTH,
    )
    assert base.status_code == withflag.status_code == 200
    ids = lambda r: [h["chunk_id"] for h in r.json()["results"]]  # noqa: E731
    assert ids(base) == ids(withflag)


@pytest.mark.asyncio
async def test_query_decompose_enabled_marks_decomposed(app_client_decompose, auth_as, fake_db) -> None:  # noqa: ANN001
    kb_id = await _seed_kb(app_client_decompose, "decompkb")
    resp = await app_client_decompose.post(
        f"/v1/kbs/{kb_id}/query",
        json={"query": "refund policy and shipping cost", "top_k": 5, "decompose": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results  # union of per-sub-question retrievals
    ids = {h["chunk_id"] for h in results}
    assert len(ids) == len(results)  # deduped by chunk_id
    usage = fake_db.outbox_payloads("cypherx.rag.usage.recorded")[-1]
    assert usage["units"]["decomposed"] is True


@pytest.mark.asyncio
async def test_query_multiquery_flag_off_is_noop(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb_id = await _seed_kb(app_client, "nomqkb")
    resp = await app_client.post(
        f"/v1/kbs/{kb_id}/query",
        json={"query": "refund policy", "top_k": 3, "multi_query": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    usage = fake_db.outbox_payloads("cypherx.rag.usage.recorded")[-1]
    assert usage["units"]["expanded"] is False


@pytest.mark.asyncio
async def test_query_multiquery_enabled_marks_expanded(app_client_multiquery, auth_as, fake_db) -> None:  # noqa: ANN001
    kb_id = await _seed_kb(app_client_multiquery, "mqkb")
    resp = await app_client_multiquery.post(
        f"/v1/kbs/{kb_id}/query",
        json={"query": "refund policy", "top_k": 3, "multi_query": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results
    # Fused RRF scores are positive and descending (rank-fusion scores, not cosine).
    scores = [h["score"] for h in results]
    assert scores == sorted(scores, reverse=True)
    usage = fake_db.outbox_payloads("cypherx.rag.usage.recorded")[-1]
    assert usage["units"]["expanded"] is True
