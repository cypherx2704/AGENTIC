"""Guardrails-service client (PRE/POST guardrail stages).

Calls ``POST /v1/check/input`` and ``POST /v1/check/output`` on the guardrails service.
Identity flows via HEADERS only (Contract 13) — the body carries NO identity:

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent`` + ``tracestate`` + ``X-Request-ID`` (Contract 8 W3C propagation,
    via ``trace.propagation_headers()`` — tracestate flows only when present)

Body: ``{ text, task_id }`` for input; ``{ text, input_text, task_id }`` for output (the
original user message lets ``output-pii-email-v1`` distinguish echo vs leak — Phase 4
post-edit). The response is normalised to ``{decision, processed_text, violations}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class GuardrailResult:
    """Normalised guardrails decision."""

    decision: str  # allow | warn | redact | block
    processed_text: str | None = None
    violations: list[dict[str, Any]] = field(default_factory=list)


class GuardrailsClient:
    """Thin async client for the guardrails check endpoints."""

    def __init__(
        self,
        settings: Settings,
        token_provider: ServiceTokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _headers(self, *, agent_jwt: str, on_behalf_of: str | None) -> dict[str, str]:
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        # W3C trace context (traceparent + tracestate + X-Request-ID) is built once via
        # the shared helper so tracestate propagates verbatim when the inbound carried it.
        return {
            "Authorization": f"Bearer {service_jwt}",
            "X-Forwarded-Agent-JWT": agent_jwt,
            **trace.propagation_headers(),
        }

    async def _check(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> GuardrailResult:
        url = f"{self._settings.guardrails_service_url.rstrip('/')}{path}"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("guardrails", "error").inc()
            logger.warning("guardrails_call_failed", path=path, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Guardrails service unavailable.") from exc
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("guardrails", "rejected").inc()
            logger.warning("guardrails_call_rejected", path=path, status=resp.status_code)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Guardrails service returned {resp.status_code}.",
            )
        data = resp.json()
        # FAIL CLOSED: a guardrail is a safety control, so a 2xx body lacking a valid decision
        # (partial deploy, schema drift, an empty 200 from a proxy) must NOT silently default to
        # 'allow' — it would let the unchecked prompt/answer through. Reject like a transport error.
        decision = data.get("decision")
        if decision not in ("allow", "warn", "redact", "block"):
            metrics.downstream_calls_total.labels("guardrails", "rejected").inc()
            logger.warning("guardrails_invalid_decision", path=path, decision=decision)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                "Guardrails returned no/invalid decision (failing closed).",
            )
        metrics.downstream_calls_total.labels("guardrails", "ok").inc()
        return GuardrailResult(
            decision=decision,
            processed_text=data.get("processed_text"),
            violations=data.get("violations", []) or [],
        )

    async def check_input(
        self,
        text: str,
        task_id: str,
        *,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> GuardrailResult:
        """PRE-LLM check. Body carries NO identity (Contract 13)."""
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        return await self._check("/v1/check/input", {"text": text, "task_id": task_id}, headers)

    async def check_output(
        self,
        text: str,
        input_text: str,
        task_id: str,
        *,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> GuardrailResult:
        """POST-LLM check. ``input_text`` enables echo-vs-leak PII logic (Phase 4 post-edit)."""
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body = {"text": text, "input_text": input_text, "task_id": task_id}
        return await self._check("/v1/check/output", body, headers)
