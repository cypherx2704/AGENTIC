"""GET /manifest — Contract-4 manifest shape + ETag / If-None-Match 304."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_manifest_shape(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.get("/manifest")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Contract-4 required top-level fields + dash-case server name.
    assert body["name"] == "tool-web-search"
    assert body["schema_version"] == "1.0.0"
    assert body["protocol_version"] == "mcp/1.0"
    assert body["version"]
    assert body["description"]
    assert body["required_scopes"] == ["tool:invoke", "tool:tool-web-search:invoke"]
    assert body["invoke_endpoint"] == "/mcp/v1/invoke"

    # Exactly one snake_case tool with an input_schema declaring `query` required.
    tools = body["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "web_search"
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert "query" in schema["properties"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["required"] == ["query"]
    assert "max_results" in schema["properties"]

    # ETag present on a 200.
    assert resp.headers.get("ETag")


@pytest.mark.asyncio
async def test_manifest_etag_if_none_match_304(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    first = await ac.get("/manifest")
    etag = first.headers["ETag"]

    # Re-poll with If-None-Match: unchanged manifest -> 304, no body.
    second = await ac.get("/manifest", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers.get("ETag") == etag
    assert second.content == b""


@pytest.mark.asyncio
async def test_manifest_etag_wildcard_304(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.get("/manifest", headers={"If-None-Match": "*"})
    assert resp.status_code == 304


@pytest.mark.asyncio
async def test_manifest_etag_mismatch_returns_200(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.get("/manifest", headers={"If-None-Match": '"not-the-current-etag"'})
    assert resp.status_code == 200
    assert resp.json()["name"] == "tool-web-search"
