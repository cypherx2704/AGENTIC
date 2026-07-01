"""Guardrails-service client (copilot pre/post screening).

Calls ``POST /v1/check/input`` and ``POST /v1/check/output``. Identity flows via HEADERS
only (Contract 13): service JWT in ``Authorization`` + forwarded agent JWT in
``X-Forwarded-Agent-JWT`` + W3C trace headers. The body carries NO identity.

FAIL CLOSED: a guardrail is a safety control, so a 2xx body lacking a valid decision — or
any 5xx/transport error — is treated as a hard failure (never silently 'allow'). The
caller maps ``decision=block`` to an app ``422 GUARDRAIL_VIOLATION``. Direct port of the
xAgent ax-1 guardrails client.
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
    decision: str  # allow | warn | redact | block
    processed_text: str | None = None
    violations: list[dict[str, Any]] = field(default_factory=list)


class GuardrailsClient:
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
            self._client = httpx.AsyncClient(timeout=self._settings.guardrails_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _headers(self, *, agent_jwt: str, on_behalf_of: str | None) -> dict[str, str]:
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
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
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"Guardrails returned {resp.status_code}.")
        data = resp.json()
        decision = data.get("decision")
        if decision not in ("allow", "warn", "redact", "block"):
            metrics.downstream_calls_total.labels("guardrails", "rejected").inc()
            logger.warning("guardrails_invalid_decision", path=path, decision=decision)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, "Guardrails returned no/invalid decision (failing closed)."
            )
        metrics.downstream_calls_total.labels("guardrails", "ok").inc()
        return GuardrailResult(
            decision=decision,
            processed_text=data.get("processed_text"),
            violations=data.get("violations", []) or [],
        )

    async def check_input(
        self, text: str, task_id: str, *, agent_jwt: str, on_behalf_of: str | None = None
    ) -> GuardrailResult:
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        return await self._check("/v1/check/input", {"text": text, "task_id": task_id}, headers)

    async def check_output(
        self, text: str, input_text: str, task_id: str, *, agent_jwt: str, on_behalf_of: str | None = None
    ) -> GuardrailResult:
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body = {"text": text, "input_text": input_text, "task_id": task_id}
        return await self._check("/v1/check/output", body, headers)
