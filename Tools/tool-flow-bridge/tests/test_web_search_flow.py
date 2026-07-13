"""The packaged web_search flow asset (Phase 5 · 5-websearch).

Validates that ``assets/web_search_flow.json`` is a real, tool-shaped Node-RED flow (exactly one
enabled http-in reachable to an http-response — the same rule the publish path enforces via
``validate_flow_shape``) and that its normalize / mock function nodes emit the tool-web-search
output shape. No Node-RED runtime is needed: we assert against the flow graph + the function source.
"""

from __future__ import annotations

from tool_flow_bridge.services.bootstrap import load_web_search_flow
from tool_flow_bridge.services.nodered_admin import validate_flow_shape


def _nodes_by_id() -> dict[str, dict]:
    flow = load_web_search_flow()
    return {n["id"]: n for n in flow["nodes"]}


def _func(node_id: str) -> str:
    return _nodes_by_id()[node_id]["func"]


# ── the asset is a valid, tool-shaped flow (drop-in for the publish path) ─────────────
def test_flow_is_tool_shaped_post_web_search() -> None:
    """Exactly one enabled http-in, reachable to an http-response, POST /web_search."""
    shape = validate_flow_shape(load_web_search_flow())
    assert shape.http_method == "POST"
    assert shape.http_path == "/web_search"


def test_flow_has_single_enabled_http_in_and_a_response() -> None:
    nodes = list(_nodes_by_id().values())
    http_ins = [n for n in nodes if n.get("type") == "http in" and not n.get("d", False)]
    responses = [n for n in nodes if n.get("type") == "http response" and not n.get("d", False)]
    assert len(http_ins) == 1
    assert len(responses) >= 1


def test_flow_has_provider_and_mock_branches() -> None:
    """A `http request` provider branch AND a keyless mock branch both reach the response."""
    nodes = _nodes_by_id()
    assert nodes["ws_request"]["type"] == "http request"
    # switch routes provider (output 1) vs mock (output 2).
    route = nodes["ws_route"]
    assert route["type"] == "switch"
    assert route["wires"] == [["ws_build"], ["ws_mock"]]
    # Both the provider-normalize and the mock branch wire into the single http response.
    assert nodes["ws_normalize"]["wires"] == [["ws_response"]]
    assert nodes["ws_mock"]["wires"] == [["ws_response"]]


# ── normalize function shape: provider payload -> {results:[{title,url,snippet,rank}]} ─
def test_normalize_emits_result_shape() -> None:
    src = _func("ws_normalize")
    for key in ("results", "title", "url", "snippet", "rank"):
        assert key in src, f"normalize must set '{key}'"
    # serpapi maps organic_results[].link/snippet/position; brave maps web.results[].url/description.
    assert "organic_results" in src
    assert "it.link" in src
    assert "it.position" in src
    assert "web.results" in src or "payload.web" in src
    assert "it.description" in src


def test_prepare_reads_query_and_count_alias() -> None:
    """prepare reads `query`, `count`, and the `max_results` drop-in alias; picks keyless mock
    when no provider key is configured."""
    src = _func("ws_prepare")
    assert "body.query" in src
    assert "body.count" in src
    assert "body.max_results" in src  # tool-web-search alias
    assert "SERPAPI_API_KEY" in src
    assert "BRAVE_SEARCH_API_KEY" in src
    assert "'mock'" in src  # default keyless mode


def test_mock_branch_is_deterministic_result_shape() -> None:
    src = _func("ws_mock")
    assert "results" in src
    assert "Result " in src  # 'Result <rank> for <query>' — mirrors tool-web-search's mock
    assert "rank" in src
