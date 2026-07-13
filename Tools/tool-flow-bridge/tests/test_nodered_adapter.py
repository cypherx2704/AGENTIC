"""Node-RED adapter tests — response mapping (respx-mocked, no real Node-RED)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tool_flow_bridge.services.nodered_adapter import NoderedError, invoke_workflow

URL = "http://nodered:1880/flow/sum"


async def _call():
    async with httpx.AsyncClient() as client:
        return await invoke_workflow(
            client,
            internal_host="http://nodered:1880",
            http_node_root="/flow",
            http_path="/sum",
            method="POST",
            args={"a": 2, "b": 3},
            secret="s",
            secret_header="X-CypherX-Tool-Secret",
            timeout=5.0,
        )


@respx.mock
async def test_success_returns_json_result():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"sum": 5}))
    assert await _call() == {"sum": 5}


@respx.mock
async def test_non_json_2xx_wrapped():
    respx.post(URL).mock(return_value=httpx.Response(200, text="ok"))
    assert await _call() == {"output": "ok"}


@respx.mock
async def test_4xx_is_terminal():
    respx.post(URL).mock(return_value=httpx.Response(422, json={"error": "bad"}))
    with pytest.raises(NoderedError) as e:
        await _call()
    assert e.value.retryable is False


@respx.mock
async def test_5xx_is_retryable():
    respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(NoderedError) as e:
        await _call()
    assert e.value.retryable is True


@respx.mock
async def test_timeout_is_retryable():
    respx.post(URL).mock(side_effect=httpx.TimeoutException("timeout"))
    with pytest.raises(NoderedError) as e:
        await _call()
    assert e.value.retryable is True


@respx.mock
async def test_secret_header_sent():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await _call()
    assert route.calls.last.request.headers["X-CypherX-Tool-Secret"] == "s"


# ── the flow's OWN error message must survive to the caller ──────────────────────────────
@respx.mock
async def test_404_surfaces_the_flows_own_message_not_a_generic_one():
    """A flow answering {"error": "topic not found"} is saying exactly what went wrong. Reporting
    only "Workflow rejected the request (HTTP 404)" sends the author hunting for a broken tool or a
    missing API key when the upstream simply had no such record.
    """
    respx.post(URL).mock(
        return_value=httpx.Response(404, json={"error": "topic not found", "status": 404})
    )
    with pytest.raises(NoderedError) as e:
        await _call()

    assert e.value.retryable is False
    assert "topic not found" in e.value.message           # the flow's own words survive
    assert "not found" in e.value.message.lower()
    assert "rejected the request" not in e.value.message  # the misleading phrasing is gone


@respx.mock
async def test_4xx_surfaces_detail_from_any_conventional_key():
    respx.post(URL).mock(return_value=httpx.Response(400, json={"message": "owner is required"}))
    with pytest.raises(NoderedError) as e:
        await _call()
    assert "owner is required" in e.value.message


@respx.mock
async def test_4xx_without_a_body_still_reports_the_status():
    respx.post(URL).mock(return_value=httpx.Response(403, text=""))
    with pytest.raises(NoderedError) as e:
        await _call()
    assert "403" in e.value.message


@respx.mock
async def test_5xx_detail_is_surfaced_and_still_retryable():
    respx.post(URL).mock(return_value=httpx.Response(502, json={"error": "upstream refused"}))
    with pytest.raises(NoderedError) as e:
        await _call()
    assert e.value.retryable is True
    assert "upstream refused" in e.value.message
