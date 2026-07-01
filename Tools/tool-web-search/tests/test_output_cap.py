"""POST /mcp/v1/invoke — 10 MiB output cap.

Uses the mock provider's ``__bloat__:<n>`` query seam to make a SINGLE result whose
snippet is N bytes, deterministically pushing the serialized response past the cap. The
service must reject (413 PAYLOAD_TOO_LARGE) rather than stream the oversized body.
"""

from __future__ import annotations

import pytest

from tool_web_search.core.config import get_settings

_INVOKE = "/mcp/v1/invoke"


@pytest.mark.asyncio
async def test_invoke_output_over_cap_rejected_413(make_client) -> None:  # type: ignore[no-untyped-def]
    cap = get_settings().max_output_bytes
    # One result with a snippet just over the cap -> serialized body exceeds it.
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"query": f"__bloat__:{cap + 1024}"}})
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert body["error"]["details"]["reason"] == "OUTPUT_BYTES_EXCEEDED"
    assert body["error"]["details"]["max_bytes"] == cap
    assert body["error"]["details"]["bytes"] > cap


@pytest.mark.asyncio
async def test_invoke_output_under_cap_ok(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.post(_INVOKE, json={"args": {"query": "__bloat__:1024"}})
    assert resp.status_code == 200, resp.text
