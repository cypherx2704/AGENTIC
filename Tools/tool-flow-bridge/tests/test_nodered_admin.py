"""Node-RED Admin service tests — flow-shape validation + best-effort redeploy.

``validate_flow_shape`` is pure (no I/O). ``redeploy_flow`` is exercised against a
respx-mocked Admin API so no live Node-RED runtime is needed.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tool_flow_bridge.core.config import Settings
from tool_flow_bridge.core.errors import ApiError, ErrorCode
from tool_flow_bridge.services.nodered_admin import (
    FlowShape,
    NoderedAdmin,
    validate_flow_shape,
)

INTERNAL_HOST = "http://nodered:1880"
FLOW_ID = "sum"


def _settings() -> Settings:
    return Settings()


# ── validate_flow_shape ────────────────────────────────────────────────────────


def test_valid_flow_returns_shape():
    flow = {
        "nodes": [
            {"id": "1", "type": "http in", "method": "post", "url": "/sum", "wires": [["2"]]},
            {"id": "2", "type": "http response"},
        ]
    }
    shape = validate_flow_shape(flow)
    assert isinstance(shape, FlowShape)
    assert shape.http_method == "POST"
    assert shape.http_path == "/sum"


def test_valid_flow_reaches_response_through_intermediate_node():
    # http in -> function -> http response (the common shape) must validate.
    flow = {
        "nodes": [
            {"id": "1", "type": "http in", "method": "post", "url": "/sum", "wires": [["fn"]]},
            {"id": "fn", "type": "function", "wires": [["2"]]},
            {"id": "2", "type": "http response"},
        ]
    }
    assert validate_flow_shape(flow).http_path == "/sum"


def test_http_response_present_but_unreachable_raises():
    # An http-response that the trigger is NOT wired to => the tool would hang; reject at publish.
    flow = {
        "nodes": [
            {"id": "1", "type": "http in", "method": "post", "url": "/sum", "wires": [[]]},
            {"id": "2", "type": "http response"},
        ]
    }
    with pytest.raises(ApiError) as exc:
        validate_flow_shape(flow)
    assert exc.value.status_code == 422
    assert exc.value.details == {"reason": "HTTP_RESPONSE_UNREACHABLE"}


def test_zero_http_in_raises_missing_http_in():
    flow = {"nodes": [{"id": "2", "type": "http response"}]}
    with pytest.raises(ApiError) as exc:
        validate_flow_shape(flow)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert exc.value.status_code == 422
    assert exc.value.details == {"reason": "MISSING_HTTP_IN"}


def test_two_http_in_raises_multiple_http_in():
    flow = {
        "nodes": [
            {"id": "1", "type": "http in", "method": "post", "url": "/a"},
            {"id": "2", "type": "http in", "method": "post", "url": "/b"},
            {"id": "3", "type": "http response"},
        ]
    }
    with pytest.raises(ApiError) as exc:
        validate_flow_shape(flow)
    assert exc.value.status_code == 422
    assert exc.value.details == {"reason": "MULTIPLE_HTTP_IN"}


def test_missing_http_response_raises():
    flow = {"nodes": [{"id": "1", "type": "http in", "method": "post", "url": "/sum"}]}
    with pytest.raises(ApiError) as exc:
        validate_flow_shape(flow)
    assert exc.value.status_code == 422
    assert exc.value.details == {"reason": "MISSING_HTTP_RESPONSE"}


def test_http_in_with_non_slash_path_raises_invalid_path():
    flow = {
        "nodes": [
            {"id": "1", "type": "http in", "method": "post", "url": "sum", "wires": [["2"]]},
            {"id": "2", "type": "http response"},
        ]
    }
    with pytest.raises(ApiError) as exc:
        validate_flow_shape(flow)
    assert exc.value.status_code == 422
    assert exc.value.details == {"reason": "INVALID_HTTP_IN_PATH"}


def test_disabled_http_in_is_ignored():
    # A disabled (d: true) http-in must not count -> only the enabled one remains.
    flow = {
        "nodes": [
            {"id": "0", "type": "http in", "method": "post", "url": "/dead", "d": True},
            {"id": "1", "type": "http in", "method": "get", "url": "/live", "wires": [["2"]]},
            {"id": "2", "type": "http response"},
        ]
    }
    shape = validate_flow_shape(flow)
    assert shape.http_method == "GET"
    assert shape.http_path == "/live"


def test_disabled_http_in_alone_raises_missing_http_in():
    # If the only http-in is disabled, the flow has effectively zero triggers.
    flow = {
        "nodes": [
            {"id": "0", "type": "http in", "method": "post", "url": "/dead", "d": True},
            {"id": "2", "type": "http response"},
        ]
    }
    with pytest.raises(ApiError) as exc:
        validate_flow_shape(flow)
    assert exc.value.details == {"reason": "MISSING_HTTP_IN"}


# ── redeploy_flow (best-effort) ────────────────────────────────────────────────


def _redeploy_url() -> str:
    root = _settings().nodered_admin_root.rstrip("/")
    return f"{INTERNAL_HOST}{root}/flow/{FLOW_ID}"


async def _redeploy() -> bool:
    settings = _settings()
    async with httpx.AsyncClient() as client:
        admin = NoderedAdmin(client, settings)
        return await admin.redeploy_flow(
            internal_host=INTERNAL_HOST,
            admin_token="tok",
            flow_id=FLOW_ID,
            flow={"id": FLOW_ID, "label": "sum", "nodes": []},
        )


@respx.mock
async def test_redeploy_204_returns_true():
    route = respx.put(_redeploy_url()).mock(return_value=httpx.Response(204))
    assert await _redeploy() is True
    assert route.called


@respx.mock
async def test_redeploy_500_returns_false_no_raise():
    respx.put(_redeploy_url()).mock(return_value=httpx.Response(500, text="boom"))
    assert await _redeploy() is False


@respx.mock
async def test_redeploy_transport_error_returns_false():
    respx.put(_redeploy_url()).mock(side_effect=httpx.ConnectError("unreachable"))
    assert await _redeploy() is False
