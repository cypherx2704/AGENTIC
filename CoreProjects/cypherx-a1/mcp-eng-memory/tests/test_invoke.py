"""POST /mcp/v1/invoke — dispatch, input-schema validation, scope + tool guards (network-free)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mcp_eng_memory.core.auth import Principal, require_principal


def test_who_owns_dispatches_to_backend(client: TestClient) -> None:
    r = client.post("/mcp/v1/invoke", json={"tool": "who_owns", "args": {"target": "acme/payments"}})
    assert r.status_code == 200
    body = r.json()
    assert body["tool"] == "who_owns"
    assert body["citations"], "tool results must be cited"
    assert client.app.state.backend.calls[0][0] == "/v1/graph/who-owns"


def test_how_does_x_work_uses_copilot(client: TestClient) -> None:
    r = client.post("/mcp/v1/invoke", json={"tool": "how_does_x_work", "args": {"topic": "payments retries"}})
    assert r.status_code == 200
    assert r.json()["output"]["answer"]
    assert client.app.state.backend.calls[0][0] == "/v1/copilot/ask"


def test_what_changed_dispatches_to_activity(client: TestClient) -> None:
    r = client.post(
        "/mcp/v1/invoke",
        json={"tool": "what_changed", "args": {"target": "acme/payments", "since": "2026-06-01T00:00:00Z"}},
    )
    assert r.status_code == 200
    assert r.json()["tool"] == "what_changed"
    assert client.app.state.backend.calls[0][0] == "/v1/graph/activity"
    assert client.app.state.backend.calls[0][1].get("since") == "2026-06-01T00:00:00Z"


def test_unknown_tool_404(client: TestClient) -> None:
    r = client.post("/mcp/v1/invoke", json={"tool": "rm_rf", "args": {}})
    assert r.status_code == 404


def test_input_schema_validation_pointer(client: TestClient) -> None:
    # missing required 'target' -> 422 with a JSON Pointer.
    r = client.post("/mcp/v1/invoke", json={"tool": "who_owns", "args": {}})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["pointer"] == "/target"


def test_additional_properties_rejected(client: TestClient) -> None:
    r = client.post("/mcp/v1/invoke", json={"tool": "who_owns", "args": {"target": "x", "evil": 1}})
    assert r.status_code == 422


class _NoopBackend:
    async def graph(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        return {}

    async def ask(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        return {}

    async def aclose(self) -> None:
        pass


def test_fine_scope_required(principal: Principal) -> None:
    # A principal with only the coarse scope must be rejected by the fine-scope check.
    from mcp_eng_memory.main import create_app

    app = create_app()
    coarse_only = Principal(tenant_id=principal.tenant_id, agent_id=principal.agent_id,
                            scopes=["tool:invoke"], agent_jwt="j")
    app.dependency_overrides[require_principal] = lambda: coarse_only
    with TestClient(app) as c:
        c.app.state.backend = _NoopBackend()
        r = c.post("/mcp/v1/invoke", json={"tool": "who_owns", "args": {"target": "x"}})
    assert r.status_code == 403
    app.dependency_overrides.clear()
