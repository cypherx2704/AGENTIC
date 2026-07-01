"""Phase B — change/activity surface + consolidation (network-free).

DB-dependent parts (consolidation idempotency, the activity_timeline SQL) are exercised by
scripts/live_graph_demo.py against a real Postgres; here we cover the pure/fakeable logic."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cypherx_a1.connectors.github import _fixture_records
from cypherx_a1.core.auth import Principal, require_principal
from cypherx_a1.extraction.consolidator import _cluster_sha
from cypherx_a1.main import create_app
from cypherx_a1.models.api import Citation


# ── connector change-granularity ──────────────────────────────────────────────
def test_commit_change_nodes_present_for_auto_and_commit() -> None:
    for gran in ("auto", "commit"):
        recs = _fixture_records(gran)
        changes = [n for r in recs for n in r.nodes if n.kind == "change"]
        assert changes, f"expected change nodes for granularity={gran}"
        # each change is authored by a person and touched a repo
        rels = {e.rel for r in recs for e in r.edges if r.record_type == "commit"}
        assert {"authored", "touched"} <= rels


def test_no_change_nodes_for_pr_ticket_granularity() -> None:
    recs = _fixture_records("pr_ticket")
    assert not [n for r in recs for n in r.nodes if n.kind == "change"]


def test_change_node_has_timestamp_and_author() -> None:
    recs = _fixture_records("commit")
    change = next(n for r in recs for n in r.nodes if n.kind == "change")
    assert "timestamp" in change.attrs and change.attrs.get("author")


# ── consolidation idempotency key ─────────────────────────────────────────────
def test_cluster_sha_is_deterministic_and_order_independent() -> None:
    a = _cluster_sha(["id2", "id1"], "1.0.0")
    b = _cluster_sha(["id1", "id2"], "1.0.0")
    assert a == b  # order-independent
    assert _cluster_sha(["id1"], "1.0.0") != _cluster_sha(["id1"], "2.0.0")  # version-sensitive


# ── /v1/graph/activity endpoint ───────────────────────────────────────────────
class _FakeGraphQueries:
    async def activity(self, *, tenant_id, target, since=None, until=None):  # noqa: ANN001, ANN201
        return (
            [{"activity": "Add Stripe webhook signature verification", "kind": "change",
              "author": "Alice Ng", "when": "2026-06-10T09:00:00+00:00"}],
            [Citation(kind="entity", title="acme/payments", entity_kind="repo")],
        )


@pytest.fixture
def activity_client(principal: Principal):  # noqa: ANN201
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: principal
    with TestClient(app) as c:
        c.app.state.graph_queries = _FakeGraphQueries()
        yield c
    app.dependency_overrides.clear()


def test_activity_endpoint_returns_cited_timeline(activity_client: TestClient) -> None:
    r = activity_client.post("/v1/graph/activity", json={"target": "acme/payments"})
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["kind"] == "change"
    assert body["items"][0]["author"] == "Alice Ng"
    assert body["citations"]


def test_activity_endpoint_accepts_since_until(activity_client: TestClient) -> None:
    r = activity_client.post(
        "/v1/graph/activity", json={"target": "acme/payments", "since": "2026-06-01T00:00:00Z"}
    )
    assert r.status_code == 200


def test_activity_endpoint_reserved_key_422(activity_client: TestClient) -> None:
    r = activity_client.post("/v1/graph/activity", json={"target": "x", "tenant_id": "evil"})
    assert r.status_code == 422
