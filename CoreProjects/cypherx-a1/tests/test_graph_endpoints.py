"""Endpoint-level tests for /v1/graph/* (auth + scope gate + response shape), with the
GraphQueryService faked so no DB is needed. Closes the endpoint-coverage gap for the graph
query surface that backs the MCP server."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cypherx_a1.core.auth import Principal, require_principal
from cypherx_a1.main import create_app
from cypherx_a1.models.api import Citation


class FakeGraphQueries:
    async def who_owns(self, *, tenant_id: str, target: str):  # noqa: ANN201
        return [{"person": "Alice", "relations": ["owns"], "confidence": 1.0, "signal": 1}], [
            Citation(kind="entity", title=target, entity_kind="repo", natural_key=target)
        ]

    async def what_breaks_if_changed(self, *, tenant_id: str, target: str, max_hops: int):  # noqa: ANN201
        return [{"entity": "acme/payments", "kind": "repo", "depth": 1, "owners": ["Alice"]}], []

    async def experts_on(self, *, tenant_id: str, topic: str):  # noqa: ANN201
        return [{"person": "Bob", "score": 2.0, "relations": ["authored"]}], []

    async def why_built(self, *, tenant_id: str, feature: str):  # noqa: ANN201
        return [{"artifact": "PR #101", "kind": "pr"}], []

    async def neighbors(self, *, tenant_id: str, target: str, hops: int, as_of: str | None = None):  # noqa: ANN201
        rel = "depends_on" if as_of is None else "depended_on_as_of"
        return [{"entity": "auth-service", "kind": "service", "rel": rel, "confidence": 1.0}], []


@pytest.fixture
def graph_client(principal: Principal):  # noqa: ANN201
    app = create_app()
    app.dependency_overrides[require_principal] = lambda: principal
    with TestClient(app) as c:
        c.app.state.graph_queries = FakeGraphQueries()
        yield c
    app.dependency_overrides.clear()


def test_who_owns_endpoint(graph_client: TestClient) -> None:
    r = graph_client.post("/v1/graph/who-owns", json={"target": "acme/payments"})
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["person"] == "Alice"
    assert body["citations"], "graph answers must be cited"


def test_what_breaks_endpoint(graph_client: TestClient) -> None:
    r = graph_client.post("/v1/graph/what-breaks", json={"target": "auth-service", "max_hops": 3})
    assert r.status_code == 200
    assert r.json()["items"][0]["entity"] == "acme/payments"


def test_experts_and_why_built_and_neighbors(graph_client: TestClient) -> None:
    assert graph_client.post("/v1/graph/experts", json={"topic": "retry"}).status_code == 200
    assert graph_client.post("/v1/graph/why-built", json={"topic": "stripe"}).status_code == 200
    assert graph_client.post("/v1/graph/neighbors", json={"target": "x", "max_hops": 2}).status_code == 200


def test_neighbors_as_of_param_is_additive_and_optional(graph_client: TestClient) -> None:
    # Without as_of the current-slice path runs (today's behavior, unchanged).
    r = graph_client.post("/v1/graph/neighbors", json={"target": "x", "max_hops": 2})
    assert r.status_code == 200
    assert r.json()["items"][0]["rel"] == "depends_on"
    # With the additive as_of timestamp the bitemporal time-travel path is taken.
    r2 = graph_client.post(
        "/v1/graph/neighbors", json={"target": "x", "max_hops": 2, "as_of": "2026-01-01T00:00:00Z"}
    )
    assert r2.status_code == 200
    assert r2.json()["items"][0]["rel"] == "depended_on_as_of"


def test_graph_reserved_key_rejected(graph_client: TestClient) -> None:
    r = graph_client.post("/v1/graph/who-owns", json={"target": "x", "tenant_id": "evil"})
    assert r.status_code == 422


def test_graph_scope_denied_without_query_scope() -> None:
    # A principal lacking any cypherx-a1 scope is rejected at the require_scope gate (403).
    app = create_app()
    noscope = Principal(tenant_id="t", agent_id="a", scopes=["something:else"], raw_token="j")
    app.dependency_overrides[require_principal] = lambda: noscope
    with TestClient(app) as c:
        c.app.state.graph_queries = FakeGraphQueries()
        r = c.post("/v1/graph/who-owns", json={"target": "x"})
    assert r.status_code == 403
    app.dependency_overrides.clear()
