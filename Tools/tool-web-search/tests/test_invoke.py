"""POST /mcp/v1/invoke — happy path, dual-scope deny, schema 422 with JSON Pointer."""

from __future__ import annotations

import pytest

from tool_web_search.services import manifest as manifest_svc

from .conftest import make_principal

_INVOKE = "/mcp/v1/invoke"


@pytest.mark.asyncio
async def test_invoke_happy_path_mock_provider(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"query": "cypherx", "max_results": 3}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool"] == "web_search"
    results = body["result"]["results"]
    assert len(results) == 3
    # Deterministic mock results carry rank/title/url/snippet.
    assert results[0]["rank"] == 1
    assert "cypherx" in results[0]["title"]
    assert results[0]["url"].startswith("https://")
    assert results[0]["snippet"]


@pytest.mark.asyncio
async def test_invoke_default_max_results(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"query": "hello"}})
    assert resp.status_code == 200, resp.text
    # Default max_results = 5 from the manifest schema.
    assert len(resp.json()["result"]["results"]) == 5


@pytest.mark.asyncio
async def test_invoke_accepts_arguments_alias(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"arguments": {"query": "alias"}})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_invoke_missing_fine_scope_403(make_client) -> None:  # type: ignore[no-untyped-def]
    # Dual-scope check (Contract-4): a caller holding only the coarse `tool:invoke` scope
    # is denied the fine-grained per-server `tool:tool-web-search:invoke` scope by the
    # handler. (The coarse-scope deny is enforced in require_principal — see test_auth.py.)
    principal = make_principal(scopes=[manifest_svc.COARSE_SCOPE])
    ac = await make_client(principal=principal)
    resp = await ac.post(_INVOKE, json={"args": {"query": "x"}})
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert manifest_svc.FINE_SCOPE in body["error"]["message"]


@pytest.mark.asyncio
async def test_invoke_no_scopes_403(make_client) -> None:  # type: ignore[no-untyped-def]
    # A principal with neither scope is denied (the handler's fine-scope gate trips first).
    principal = make_principal(scopes=[])
    ac = await make_client(principal=principal)
    resp = await ac.post(_INVOKE, json={"args": {"query": "x"}})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_invoke_missing_query_422_json_pointer(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"max_results": 3}})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["pointer"] == "/query"


@pytest.mark.asyncio
async def test_invoke_wrong_typed_query_422_json_pointer(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"query": 123}})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["pointer"] == "/query"


@pytest.mark.asyncio
async def test_invoke_max_results_over_max_422_pointer(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"query": "x", "max_results": 999}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["pointer"] == "/max_results"


@pytest.mark.asyncio
async def test_invoke_unknown_field_422_pointer(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"query": "x", "bogus": 1}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["pointer"] == "/bogus"


@pytest.mark.asyncio
async def test_invoke_unknown_tool_404(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"tool": "not_web_search", "args": {"query": "x"}})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
