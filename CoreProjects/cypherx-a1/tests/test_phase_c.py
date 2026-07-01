"""Phase C — query-type classifier + per-leg weights + DoK recency decay (pure, network-free).

The DB-dependent expert_in / ownership-concentration writes are exercised by
scripts/live_graph_demo.py against a real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cypherx_a1.extraction.expertise import _decay
from cypherx_a1.retrieval.query_classifier import classify, leg_weights


def test_classifier_buckets() -> None:
    assert classify("Who owns acme/payments?") == "ownership"
    assert classify("What breaks if I change auth-service?") == "dependency"
    assert classify("Who is the expert on Kafka?") == "expertise"
    assert classify("What changed in payments recently?") == "timeline"
    assert classify("Why was the Stripe webhook built?") == "reasoning"
    assert classify("payments") == "general"
    assert classify("") == "general"


def test_leg_weights_shapes_intent() -> None:
    # ownership/dependency lean on the graph leg; reasoning leans on the rag leg.
    g_own, _, r_own = leg_weights("ownership")
    g_rea, _, r_rea = leg_weights("reasoning")
    assert g_own > r_own          # ownership boosts graph over rag
    assert r_rea > g_rea          # reasoning boosts rag over graph
    assert leg_weights("general") == (1.0, 1.0, 1.0)
    assert leg_weights("nonsense") == (1.0, 1.0, 1.0)


def test_decay_is_recency_monotonic() -> None:
    now = datetime(2026, 6, 14, tzinfo=UTC)
    recent = _decay(now - timedelta(days=10), now, 180.0)
    old = _decay(now - timedelta(days=720), now, 180.0)
    assert recent > old
    assert 0.0 < old <= recent <= 1.0
    # exactly one half-life ⇒ ~0.5
    assert abs(_decay(now - timedelta(days=180), now, 180.0) - 0.5) < 0.02
    # unknown time ⇒ a mild, non-zero default (never drops the signal entirely)
    assert _decay(None, now, 180.0) == 0.5
