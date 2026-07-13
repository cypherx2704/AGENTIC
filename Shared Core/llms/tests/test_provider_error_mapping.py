"""Upstream-provider exception -> Contract-2 ApiError mapping (hardening, 2026-07-05).

Guards ``services/providers/provider_errors.map_provider_exception`` and the OpenAI
adaptor's ``_provider_label`` derivation. Historically every OpenRouter/OpenAI-
compatible failure collapsed into an opaque 503 "provider call failed"; this suite
locks in the correct per-category remap (429/401/402/422/400/503/504) plus curated,
provider-named messages with the raw upstream reason preserved in ``details``.

Duck-typed fake SDK exceptions stand in for the real openai/anthropic SDK error
classes (which carry ``status_code`` / ``code`` / ``body`` / ``response``), so the
suite runs with no SDK installed and no network.
"""

from __future__ import annotations

import pytest

from llms_gateway.core.errors import ErrorCode
from llms_gateway.services.providers.openai_provider import OpenAIProvider
from llms_gateway.services.providers.provider_errors import map_provider_exception


class _FakeResp:
    def __init__(self, status: int | None, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class _FakeSDKError(Exception):
    """Mimics the openai/anthropic SDK error surface used by the mapper."""

    def __init__(
        self,
        status: int | None = None,
        code: str | None = None,
        body: dict | None = None,
        headers: dict | None = None,
        msg: str = "upstream error",
    ) -> None:
        super().__init__(msg)
        if status is not None:
            self.status_code = status
        if code is not None:
            self.code = code
        if body is not None:
            self.body = body
        self.response = _FakeResp(status, headers) if status is not None else None


@pytest.mark.parametrize(
    ("exc", "want_code", "want_status"),
    [
        (_FakeSDKError(429, body={"error": {"message": "Rate limit exceeded"}}),
         ErrorCode.RATE_LIMIT_EXCEEDED, 429),
        (_FakeSDKError(402, body={"error": {"message": "Insufficient credits"}}),
         ErrorCode.BUDGET_EXCEEDED, 402),
        # quota code without a 402 status -> still a budget problem, not an outage.
        (_FakeSDKError(400, code="insufficient_quota"),
         ErrorCode.BUDGET_EXCEEDED, 402),
        (_FakeSDKError(401, body={"error": {"message": "No auth credentials found"}}),
         ErrorCode.UNAUTHORIZED, 401),
        (_FakeSDKError(403), ErrorCode.FORBIDDEN, 403),
        (_FakeSDKError(404, body={"error": {"message": "model not found"}}),
         ErrorCode.MODEL_UNSUPPORTED, 422),
        (_FakeSDKError(400, body={"error": {"message": "max_tokens too large"}}),
         ErrorCode.VALIDATION_ERROR, 400),
        (_FakeSDKError(408), ErrorCode.SERVICE_UNAVAILABLE, 504),
        (_FakeSDKError(500, body={"error": {"message": "internal"}}),
         ErrorCode.SERVICE_UNAVAILABLE, 503),
        # connection/timeout: no HTTP status ever reached -> genuine outage.
        (_FakeSDKError(msg="Connection refused"), ErrorCode.SERVICE_UNAVAILABLE, 503),
    ],
)
def test_maps_upstream_status_to_contract2_code(exc, want_code, want_status):
    err = map_provider_exception(exc, provider="openrouter", model_id="acme/model:free")
    assert err.code == want_code
    assert err.status_code == want_status
    # Curated headline names the real provider; details carry the raw upstream context.
    assert "openrouter" in err.message
    assert err.details["provider"] == "openrouter"
    assert err.details["model"] == "acme/model:free"
    assert "upstream_status" in err.details


def test_rate_limit_passes_through_retry_after():
    exc = _FakeSDKError(429, headers={"retry-after": "12"})
    err = map_provider_exception(exc, provider="openrouter", model_id="m")
    assert err.headers == {"Retry-After": "12"}


def test_400_includes_upstream_reason_but_404_does_not_leak_body():
    bad = map_provider_exception(
        _FakeSDKError(400, body={"error": {"message": "max_tokens too large"}}),
        provider="together", model_id="m",
    )
    # 400 is genuinely useful to echo the provider reason.
    assert "max_tokens too large" in bad.message
    # 404 uses a curated message (no verbatim body in the headline), reason in details.
    retired = map_provider_exception(
        _FakeSDKError(404, body={"error": {"message": "internal-routing-detail xyz"}}),
        provider="together", model_id="m",
    )
    assert "internal-routing-detail" not in retired.message
    assert retired.details["upstream_message"] == "internal-routing-detail xyz"


def test_provider_label_from_base_url():
    assert OpenAIProvider("k", "https://openrouter.ai/api/v1")._provider_label() == "openrouter"
    assert OpenAIProvider("k", "https://api.together.xyz/v1")._provider_label() == "together"
    assert OpenAIProvider("k", None)._provider_label() == "openai"
    # Unknown host: strip api./www. but keep something meaningful.
    assert OpenAIProvider("k", "https://my-vllm.internal:8000/v1")._provider_label() == (
        "my-vllm.internal:8000"
    )
