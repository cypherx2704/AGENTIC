"""BUG 2 — surface the llms-gateway upstream 4xx code instead of masking it.

Before: ``LlmsClient.chat`` collapsed EVERY gateway non-2xx into
``SERVICE_UNAVAILABLE``, so a 422 ``MODEL_UNSUPPORTED`` (the agent's configured model is
not supported — a client/config error) looked like a transient availability outage. That
misled operators and clients, and (combined with the retry posture) made a permanent
config error masquerade as a retryable one.

After:
  * a 4xx surfaces the upstream Contract-2 ``code`` (+ message) on the ApiError, which the
    LLM stage records on the task result (``error_code = MODEL_UNSUPPORTED``);
  * **429** surfaces as ``RATE_LIMIT_EXCEEDED`` (NOT SERVICE_UNAVAILABLE), preserving the
    gateway's message + a passed-through ``Retry-After`` — a rate-limit is not an outage;
  * **402** surfaces as ``BUDGET_EXCEEDED`` (insufficient provider credit);
  * a 5xx (and 408) still maps to SERVICE_UNAVAILABLE (genuine availability).

Layer 1 tests ``LlmsClient.chat`` over respx. Layer 2 drives the REAL ``LlmStage`` with a
fake LLMs client that raises the surfaced ApiError, asserting the task fails with the
upstream code (not SERVICE_UNAVAILABLE).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
import respx

from agent_runtime.core.auth import Principal
from agent_runtime.core.config import get_settings
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.llm import LlmStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.services.llms_client import LlmsClient
from agent_runtime.services.service_token import ServiceTokenProvider

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"
TEST_AGENT_JWT = "test.inbound.agent-jwt"


def _mock_service_token(router: respx.Router) -> None:
    s = get_settings()
    router.post(f"{s.auth_service_url.rstrip('/')}/v1/service-tokens").mock(
        return_value=httpx.Response(200, json={"access_token": "svc.jwt.token", "expires_in": 300})
    )


async def _call_chat_with_gateway_response(gateway: httpx.Response) -> ApiError:
    """Drive LlmsClient.chat against a mocked gateway response; return the raised ApiError."""
    s = get_settings()
    tokens = ServiceTokenProvider(s)
    llms = LlmsClient(s, tokens)
    try:
        with pytest.raises(ApiError) as ei:
            await llms.chat(
                model="some-model",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=64,
                agent_jwt=TEST_AGENT_JWT,
                on_behalf_of=AGENT,
            )
        return ei.value
    finally:
        await llms.aclose()
        await tokens.aclose()


# ── Layer 1: client error mapping ───────────────────────────────────────────────────
@pytest.mark.asyncio
@respx.mock
async def test_gateway_422_model_unsupported_surfaces_upstream_code() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(
            422,
            json={
                "error": {
                    "code": "MODEL_UNSUPPORTED",
                    "message": "Model 'some-model' is not supported.",
                }
            },
        )
    )

    exc = await _call_chat_with_gateway_response(httpx.Response(422))  # response above wins
    assert exc.code == "MODEL_UNSUPPORTED"  # NOT collapsed to SERVICE_UNAVAILABLE
    assert exc.status_code == 422
    assert "not supported" in exc.message


@pytest.mark.asyncio
@respx.mock
async def test_gateway_400_flat_envelope_surfaces_code() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    # Flat (non-nested) body is tolerated too.
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"code": "VALIDATION_ERROR", "message": "bad body"})
    )
    exc = await _call_chat_with_gateway_response(httpx.Response(400))
    assert exc.code == "VALIDATION_ERROR"
    assert exc.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_gateway_4xx_unparsable_body_falls_back_to_validation_error() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(403, text="not json")
    )
    exc = await _call_chat_with_gateway_response(httpx.Response(403))
    # A 4xx with no usable envelope is still a client-family error (not availability).
    assert exc.code == ErrorCode.VALIDATION_ERROR
    assert exc.status_code == 403


@pytest.mark.asyncio
@respx.mock
async def test_gateway_500_still_maps_to_service_unavailable() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR", "message": "boom"}})
    )
    exc = await _call_chat_with_gateway_response(httpx.Response(500))
    assert exc.code == ErrorCode.SERVICE_UNAVAILABLE  # 5xx is a genuine availability error
    assert exc.status_code == 503


@pytest.mark.asyncio
@respx.mock
async def test_gateway_429_surfaces_rate_limit_not_service_unavailable() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "12"},
            json={"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "provider rate-limited (free tier)"}},
        )
    )
    exc = await _call_chat_with_gateway_response(httpx.Response(429))
    # A rate-limit is NOT an outage — surface the accurate code + message, not SERVICE_UNAVAILABLE.
    assert exc.code == ErrorCode.RATE_LIMIT_EXCEEDED
    assert exc.status_code == 429
    assert "rate-limited" in exc.message
    assert exc.headers == {"Retry-After": "12"}  # passed through for client retry logic


@pytest.mark.asyncio
@respx.mock
async def test_gateway_429_without_envelope_defaults_to_rate_limit() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(429, text="Too Many Requests")  # no Contract-2 envelope
    )
    exc = await _call_chat_with_gateway_response(httpx.Response(429))
    assert exc.code == ErrorCode.RATE_LIMIT_EXCEEDED
    assert exc.status_code == 429
    assert exc.headers is None  # no Retry-After header on this response


@pytest.mark.asyncio
@respx.mock
async def test_gateway_402_surfaces_budget_exceeded() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(
            402, json={"error": {"code": "BUDGET_EXCEEDED", "message": "insufficient credit"}}
        )
    )
    exc = await _call_chat_with_gateway_response(httpx.Response(402))
    assert exc.code == ErrorCode.BUDGET_EXCEEDED
    assert exc.status_code == 402


@pytest.mark.asyncio
@respx.mock
async def test_gateway_408_still_maps_to_service_unavailable() -> None:
    router = respx.mock
    _mock_service_token(router)
    s = get_settings()
    router.post(f"{s.llms_gateway_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=httpx.Response(408, json={"error": {"code": "SERVICE_UNAVAILABLE"}})
    )
    exc = await _call_chat_with_gateway_response(httpx.Response(408))
    assert exc.code == ErrorCode.SERVICE_UNAVAILABLE  # upstream timeout is a genuine availability problem
    assert exc.status_code == 503


# ── Layer 2: the LLM stage records the surfaced code on the task (not SERVICE_UNAVAILABLE) ──
class _RaisingLlms:
    def __init__(self, exc: ApiError) -> None:
        self._exc = exc

    async def chat(self, **_: Any) -> Any:
        raise self._exc


def _make_ctx() -> PipelineContext:
    return PipelineContext(
        principal=Principal(
            tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="agent.jwt"
        ),
        inbound_agent_jwt="agent.jwt",
        trace_id=TRACE_ID,
        request_id="req-1",
        task=TaskRow(
            task_id=TASK_ID,
            agent_id=AGENT,
            tenant_id=TENANT,
            trace_id=TRACE_ID,
            status="running",
            input={"message": "hello"},
        ),
        agent=AgentRuntime(
            agent_id=AGENT, tenant_id=TENANT, name="Test Agent", system_prompt="You are helpful."
        ),
        messages=[{"role": "user", "content": "hello"}],
        steps=StepBuffer(),
        started_monotonic=time.monotonic(),
        started_at="2026-06-10T12:00:00.000Z",
    )


@pytest.mark.asyncio
async def test_llm_stage_surfaces_model_unsupported_on_task() -> None:
    original_g, original_l = deps._guardrails_client, deps._llms_client
    surfaced = ApiError("MODEL_UNSUPPORTED", "Model not supported.", status_code=422)
    deps.set_clients(guardrails_client=None, llms_client=_RaisingLlms(surfaced))  # type: ignore[arg-type]
    try:
        ctx = _make_ctx()
        await LlmStage().run(ctx)
    finally:
        deps.set_clients(guardrails_client=original_g, llms_client=original_l)

    assert ctx.terminal_error is not None
    # The model/config error is surfaced on the task — NOT masked as availability.
    assert ctx.terminal_error.code == "MODEL_UNSUPPORTED"
    assert ctx.terminal_error.status == "failed"  # a 4xx config error fails (is not a timeout)
    # The llm_call audit step is still recorded as failed.
    assert ctx.steps is not None and ctx.steps.steps[-1].status == "failed"


@pytest.mark.asyncio
async def test_llm_stage_5xx_still_service_unavailable() -> None:
    original_g, original_l = deps._guardrails_client, deps._llms_client
    surfaced = ApiError(ErrorCode.SERVICE_UNAVAILABLE, "LLMs gateway returned 500.")
    deps.set_clients(guardrails_client=None, llms_client=_RaisingLlms(surfaced))  # type: ignore[arg-type]
    try:
        ctx = _make_ctx()
        await LlmStage().run(ctx)
    finally:
        deps.set_clients(guardrails_client=original_g, llms_client=original_l)

    assert ctx.terminal_error is not None
    assert ctx.terminal_error.code == ErrorCode.SERVICE_UNAVAILABLE
    assert ctx.terminal_error.status == "failed"
