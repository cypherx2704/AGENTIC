"""Eval-harness extensions: Context-Precision@k (B1) + decompose/multi_query rankers (B2/B3).

Exercises the offline in-process harness only (``tests/fakes`` + mock embedder/reranker, no
infra). Confirms: the ID-based MAP metric is arithmetically correct + gated; the decompose
ranker co-retrieves multi-hop facts scattered across separate docs; the multi-query ranker
recovers vocabulary-mismatch misses via RRF fusion — each measured on its own golden slice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eval.run_eval import (
    DEFAULT_CONFIGS,
    KS,
    _adapter_for,
    _context_precision_at_k,
    _mrr,
    _rank_decompose,
    _rank_hybrid,
    _rank_hybrid_rerank,
    _rank_multiquery,
    _recall_at_k,
    evaluate,
    main,
)

_GOLDEN = Path(__file__).resolve().parent.parent / "eval" / "golden_kb.json"


def _golden() -> dict:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


# ── B1: Context-Precision (ID-based MAP) arithmetic ─────────────────────────────────
def test_context_precision_perfect_ranking_is_one() -> None:
    # Both relevant chunks at the top ranks → Precision@1 = Precision@2 = 1 → CP = 1.0.
    assert _context_precision_at_k(["r1", "r2", "x", "y"], {"r1", "r2"}, 5) == pytest.approx(1.0)


def test_context_precision_penalizes_distractor_between_relevants() -> None:
    # Relevant at ranks 1 and 3 → (1/1 + 2/3) / 2 = 0.8333: a distractor at rank 2 lowers it.
    got = _context_precision_at_k(["r1", "x", "r2", "y"], {"r1", "r2"}, 5)
    assert got == pytest.approx((1.0 + 2.0 / 3.0) / 2.0)


def test_context_precision_zero_when_no_relevant_in_window() -> None:
    assert _context_precision_at_k(["x", "y", "z"], {"r1"}, 3) == 0.0
    assert _context_precision_at_k(["x", "r1"], {"r1"}, 1) == 0.0  # relevant sits just past k
    assert _context_precision_at_k(["r1"], set(), 3) == 0.0  # no relevant set


def test_context_precision_collapses_to_mrr_on_single_relevant() -> None:
    # MAP with one relevant item == reciprocal rank of that item (the documented equivalence).
    for ranked in (["r1", "x"], ["x", "r1", "y"], ["a", "b", "r1"]):
        assert _context_precision_at_k(ranked, {"r1"}, 5) == pytest.approx(_mrr(ranked, {"r1"}))


@pytest.mark.asyncio
async def test_evaluate_reports_context_precision_for_base_configs() -> None:
    rows = {r.config: r for r in await evaluate(_golden())}
    # Default configs unchanged (the CI contract) — decompose/multi_query are NOT in the default.
    assert set(rows) == set(DEFAULT_CONFIGS)
    for r in rows.values():
        assert set(r.cp) == set(KS)
        assert all(0.0 <= r.cp[k] <= 1.0 for k in KS)
    # Rerank sharpens window precision: hybrid+rerank CP@5 ≥ hybrid CP@5 (the --assert gate).
    assert rows["hybrid+rerank"].cp[5] >= rows["hybrid"].cp[5]


def test_assert_rerank_precision_gate_passes_on_golden() -> None:
    # Both regression gates return 0 (pass) on the shipped golden set.
    assert main(["--assert-rerank-precision"]) == 0
    assert main(["--assert-hybrid-ge-dense", "--assert-rerank-precision"]) == 0


# ── B2: decompose ranker (multi-hop co-retrieval) ───────────────────────────────────
@pytest.mark.asyncio
async def test_decompose_co_retrieves_scattered_facts() -> None:
    golden = _golden()
    (row,) = await evaluate(golden, configs=("decompose",))
    assert row.config == "decompose"
    # The two supporting chunks live in SEPARATE docs; decomposition co-retrieves BOTH.
    assert row.recall[10] == pytest.approx(1.0)
    assert row.recall[3] == pytest.approx(1.0)
    assert all(0.0 <= row.ndcg[k] <= 1.0 for k in KS)
    assert all(0.0 <= row.cp[k] <= 1.0 for k in KS)


@pytest.mark.asyncio
async def test_decompose_never_regresses_vs_single_query() -> None:
    # Honest measurement bound under the non-semantic mock embedder: decomposition must never
    # do WORSE than the single-query hybrid+rerank baseline on the same multi-hop slice.
    golden = _golden()
    adapter = _adapter_for(golden["multihop"]["documents"])
    queries = golden["multihop"]["queries"]
    for k in KS:
        single = 0.0
        decomp = 0.0
        for q in queries:
            rel = set(q["relevant"])
            single += _recall_at_k(await _rank_hybrid_rerank(adapter, q, top_k=max(KS)), rel, k)
            decomp += _recall_at_k(await _rank_decompose(adapter, q, top_k=max(KS)), rel, k)
        assert decomp + 1e-9 >= single


# ── B3: multi_query ranker (RAG-Fusion recall lever) ────────────────────────────────
@pytest.mark.asyncio
async def test_multiquery_lifts_recall_over_single_query() -> None:
    # The original wording is a vocabulary-mismatch miss; authored variants + RRF fusion recover
    # the relevant chunks. Demonstrates a STRICT recall lift over single-query hybrid.
    golden = _golden()
    adapter = _adapter_for(golden["multiquery"]["documents"])
    queries = golden["multiquery"]["queries"]
    single5 = 0.0
    multi5 = 0.0
    for q in queries:
        rel = set(q["relevant"])
        single5 += _recall_at_k(await _rank_hybrid(adapter, q, top_k=max(KS)), rel, 5)
        multi5 += _recall_at_k(await _rank_multiquery(adapter, q, top_k=max(KS)), rel, 5)
    assert multi5 > single5  # fusion strictly recovers vocabulary-mismatch misses


@pytest.mark.asyncio
async def test_multiquery_ranker_metrics_valid() -> None:
    golden = _golden()
    (row,) = await evaluate(golden, configs=("multi_query",))
    assert row.config == "multi_query"
    assert row.recall[10] == pytest.approx(1.0)
    assert 0.0 <= row.mrr <= 1.0
    assert all(0.0 <= row.recall[k] <= 1.0 for k in KS)
