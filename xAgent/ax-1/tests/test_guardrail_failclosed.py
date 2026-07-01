"""Regression: the xAgent guardrail path FAILS CLOSED.

A guardrail is a safety control. A 2xx response from the guardrails service that lacks a
valid ``decision`` (partial deploy, schema drift, an empty 200 from a proxy), or carries an
unknown decision, must NOT default to ``allow`` — that would silently let the unchecked
prompt/answer through. The client rejects such bodies like a transport error
(SERVICE_UNAVAILABLE), which short-circuits the task. These paths are exactly the ones the
local StubClassifier never produces, so they were invisible to the smoke test.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from agent_runtime.core.config import get_settings
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.services.guardrails_client import GuardrailsClient
from agent_runtime.services.service_token import ServiceTokenProvider

TASK_ID = "11111111-1111-1111-1111-111111111111"


def _mock_service_token(router: respx.Router) -> None:
    s = get_settings()
    router.post(f"{s.auth_service_url.rstrip('/')}/v1/service-tokens").mock(
        return_value=httpx.Response(200, json={"access_token": "svc.jwt", "expires_in": 300})
    )


@respx.mock
async def test_check_input_missing_decision_fails_closed() -> None:
    s = get_settings()
    router = respx.mock
    _mock_service_token(router)
    # 2xx but NO decision field — must NOT be treated as allow.
    router.post(f"{s.guardrails_service_url.rstrip('/')}/v1/check/input").mock(
        return_value=httpx.Response(200, json={"processed_text": None, "violations": []})
    )
    client = GuardrailsClient(s, ServiceTokenProvider(s))
    try:
        with pytest.raises(ApiError) as ei:
            await client.check_input("hello", TASK_ID, agent_jwt="agent.jwt")
        assert ei.value.code == ErrorCode.SERVICE_UNAVAILABLE
    finally:
        await client.aclose()


@respx.mock
async def test_check_output_invalid_decision_fails_closed() -> None:
    s = get_settings()
    router = respx.mock
    _mock_service_token(router)
    # 2xx with an out-of-enum decision — must NOT be accepted.
    router.post(f"{s.guardrails_service_url.rstrip('/')}/v1/check/output").mock(
        return_value=httpx.Response(200, json={"decision": "bogus"})
    )
    client = GuardrailsClient(s, ServiceTokenProvider(s))
    try:
        with pytest.raises(ApiError) as ei:
            await client.check_output("answer", "original", TASK_ID, agent_jwt="agent.jwt")
        assert ei.value.code == ErrorCode.SERVICE_UNAVAILABLE
    finally:
        await client.aclose()


@respx.mock
async def test_check_input_valid_decision_passes_through() -> None:
    s = get_settings()
    router = respx.mock
    _mock_service_token(router)
    router.post(f"{s.guardrails_service_url.rstrip('/')}/v1/check/input").mock(
        return_value=httpx.Response(200, json={"decision": "allow", "processed_text": None, "violations": []})
    )
    client = GuardrailsClient(s, ServiceTokenProvider(s))
    try:
        result = await client.check_input("hello", TASK_ID, agent_jwt="agent.jwt")
        assert result.decision == "allow"
    finally:
        await client.aclose()
