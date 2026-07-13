"""POST /mcp — real-MCP (JSON-RPC 2.0 / Streamable HTTP) transport for mcp-eng-memory.

Migrated from the legacy /mcp/v1/invoke tests: lifecycle, discovery (all 8 tools), dispatch
to the cypherx-a1 backend, input-schema validation, and the fine-scope guard — all through
the MCP wire (network-free: require_principal overridden, backend faked).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mcp_eng_memory.core.auth import Principal, require_principal
from mcp_eng_memory.services import mcp_protocol


def _rpc(method: str, params: dict | None = None, msg_id: int | str | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return msg


def _call(name: str, arguments: dict) -> dict:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


# ── Lifecycle + discovery ───────────────────────────────────────────────────────
def test_initialize(client: TestClient) -> None:
    r = client.post("/mcp", json=_rpc("initialize", {"protocolVersion": "2025-06-18"}))
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "mcp-eng-memory"


def test_tools_list_exposes_all_eight(client: TestClient) -> None:
    r = client.post("/mcp", json=_rpc("tools/list"))
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "who_owns", "why_built", "what_breaks_if_changed", "experts_on",
        "graph_neighbors", "what_changed", "incident_root_cause", "how_does_x_work",
    }
    # camelCase inputSchema per MCP.
    who = next(t for t in r.json()["result"]["tools"] if t["name"] == "who_owns")
    assert who["inputSchema"]["required"] == ["target"]


# ── Invocation ──────────────────────────────────────────────────────────────────
def test_who_owns_dispatches_to_backend(client: TestClient) -> None:
    r = client.post("/mcp", json=_call("who_owns", {"target": "acme/payments"}))
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["citations"], "tool results must be cited"
    assert client.app.state.backend.calls[0][0] == "/v1/graph/who-owns"


def test_how_does_x_work_uses_copilot(client: TestClient) -> None:
    r = client.post("/mcp", json=_call("how_does_x_work", {"topic": "payments retries"}))
    result = r.json()["result"]
    assert result["structuredContent"]["output"]["answer"]
    assert client.app.state.backend.calls[0][0] == "/v1/copilot/ask"


def test_what_changed_dispatches_to_activity(client: TestClient) -> None:
    r = client.post(
        "/mcp", json=_call("what_changed", {"target": "acme/payments", "since": "2026-06-01T00:00:00Z"})
    )
    assert r.json()["result"]["isError"] is False
    assert client.app.state.backend.calls[0][0] == "/v1/graph/activity"
    assert client.app.state.backend.calls[0][1].get("since") == "2026-06-01T00:00:00Z"


def test_unknown_tool_is_protocol_error(client: TestClient) -> None:
    r = client.post("/mcp", json=_call("rm_rf", {}))
    assert r.json()["error"]["code"] == mcp_protocol.INVALID_PARAMS


def test_input_schema_validation_pointer(client: TestClient) -> None:
    r = client.post("/mcp", json=_call("who_owns", {}))  # missing required 'target'
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["_meta"]["code"] == "VALIDATION_ERROR"
    assert result["_meta"]["pointer"] == "/target"


def test_additional_properties_rejected(client: TestClient) -> None:
    r = client.post("/mcp", json=_call("who_owns", {"target": "x", "evil": 1}))
    assert r.json()["result"]["isError"] is True


class _NoopBackend:
    async def graph(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        return {}

    async def ask(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        return {}

    async def aclose(self) -> None:
        pass


def test_fine_scope_required(principal: Principal) -> None:
    # Coarse scope present but the fine per-server scope absent -> in-band isError.
    from mcp_eng_memory.main import create_app

    app = create_app()
    coarse_only = Principal(tenant_id=principal.tenant_id, agent_id=principal.agent_id,
                            scopes=["tool:invoke"], agent_jwt="j")
    app.dependency_overrides[require_principal] = lambda: coarse_only
    with TestClient(app) as c:
        c.app.state.backend = _NoopBackend()
        r = c.post("/mcp", json=_call("who_owns", {"target": "x"}))
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["_meta"]["code"] == "FORBIDDEN"
    app.dependency_overrides.clear()
