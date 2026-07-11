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
