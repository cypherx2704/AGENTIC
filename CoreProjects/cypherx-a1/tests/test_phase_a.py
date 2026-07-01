"""Phase A — confidence floor + graph-aware rerank (pure, network-free).

The DB-dependent parts (supersedes_edge_id chain, rerank ordering over real rows) are
exercised by scripts/live_graph_demo.py against a real Postgres; here we unit-test the pure
logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cypherx_a1.extraction.extractor import _parse_edges
from cypherx_a1.retrieval.orchestrator import rerank_multiplier

_EDGES = '{"edges":[{"rel":"depends_on","target_kind":"service","target_key":"auth-service","confidence":0.9,"evidence":"x"},{"rel":"depends_on","target_kind":"service","target_key":"flaky","confidence":0.3,"evidence":"y"}]}'


def test_confidence_floor_flag_mode_keeps_but_flags() -> None:
    edges = _parse_edges(_EDGES, floor=0.6, mode="flag")
    assert len(edges) == 2
    by_key = {e["target_key"]: e for e in edges}
    assert by_key["auth-service"]["flagged"] is False
    assert by_key["flaky"]["flagged"] is True  # below 0.6 -> retained but flagged


def test_confidence_floor_drop_mode_removes_low() -> None:
    edges = _parse_edges(_EDGES, floor=0.6, mode="drop")
    assert [e["target_key"] for e in edges] == ["auth-service"]


def test_parse_edges_tolerant_of_garbage() -> None:
    assert _parse_edges(None) == []
    assert _parse_edges("not json") == []
    assert _parse_edges('{"edges":"nope"}') == []


def test_rerank_high_confidence_recent_outranks_low_stale() -> None:
    now = datetime(2026, 6, 14, tzinfo=UTC)
    recent = now - timedelta(days=1)
    stale = now - timedelta(days=365)
    kw = dict(now=now, w_conf=1.0, w_rec=0.5, halflife=90.0)
    hi = rerank_multiplier(0.95, recent, **kw)
    lo = rerank_multiplier(0.30, stale, **kw)
    assert hi > lo
    # A high-confidence current edge beats a low-confidence one even at equal recency.
    assert rerank_multiplier(0.95, recent, **kw) > rerank_multiplier(0.30, recent, **kw)
    # Recency matters: same confidence, fresher wins.
    assert rerank_multiplier(0.8, recent, **kw) > rerank_multiplier(0.8, stale, **kw)


def test_rerank_recency_weight_zero_ignores_age() -> None:
    now = datetime(2026, 6, 14, tzinfo=UTC)
    stale = now - timedelta(days=999)
    a = rerank_multiplier(0.8, stale, now=now, w_conf=1.0, w_rec=0.0, halflife=90.0)
    b = rerank_multiplier(0.8, now, now=now, w_conf=1.0, w_rec=0.0, halflife=90.0)
    assert a == b  # w_rec=0 -> recency term is a no-op


def test_rerank_handles_missing_created_at() -> None:
    now = datetime(2026, 6, 14, tzinfo=UTC)
    # No timestamp (e.g. a RAG chunk) -> recency neutral, no crash.
    m = rerank_multiplier(1.0, None, now=now, w_conf=1.0, w_rec=0.5, halflife=90.0)
    assert m > 0
