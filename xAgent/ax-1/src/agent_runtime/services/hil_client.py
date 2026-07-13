"""Human-in-the-Loop client (Phase 6) — pauses an ``ask``-mode tool/skill for user approval.

When the tool-loop hits an ``ask``-mode tool it calls :meth:`HilClient.request_and_wait`, which:

  1. POSTs to Auth ``/v1/hil/approvals/request`` (authenticated with the inbound agent JWT — Auth
     reads tenant_id/agent_id from it and keys the decision off the controlling orchestrator's HIL
     mode). The response is ``auto_approved`` (proceed now) or a pending ``request_id``.
  2. If pending, polls ``GET /v1/hil/approvals/{id}`` until the request is granted / denied / expired,
     or the local wait budget elapses.

FAIL-CLOSED: any Auth error, a denied/expired request, or a wait-timeout returns ``False`` — an
``ask`` action never runs without an explicit grant. The agent JWT is forwarded verbatim as the
Bearer (Auth's CallerContext authenticates the agent from it; no service token needed here).

Two verdict shapes, deliberately:

  * :meth:`request_and_wait` -> ``bool``. The fail-closed gate the TOOL-LOOP uses. Anything that is
    not an explicit grant is ``False``.
  * :meth:`request_verdict` -> :class:`HilVerdict`. The same round-trip, but it distinguishes an
    explicit **DENIED** from **UNAVAILABLE** (HIL off, Auth erroring, request expired, wait budget
    elapsed). Callers that must treat "the human said no" differently from "the human could not be
    reached" need this — the orchestration plan-repair gate does: a denial hard-fails the run,
    while an unreachable HIL retries anyway rather than stranding it.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any

import httpx
import structlog

from ..core import trace
from ..core.config import Settings

logger = structlog.get_logger(__name__)


class HilVerdict(StrEnum):
    """The outcome of a HIL approval round-trip.

    ``GRANTED`` — explicitly approved (or auto-approved by the orchestrator's HIL mode).
    ``DENIED``  — a human explicitly said no. The ONLY verdict that carries a human's intent to stop.
    ``UNAVAILABLE`` — no verdict could be obtained: HIL is disabled, Auth errored, the request
    expired, or the local wait budget elapsed. Distinct from ``DENIED`` because "nobody answered"
    is not "somebody refused".
    """

    GRANTED = "granted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class HilClient:
    """Async client for the Auth HIL approval endpoints (request + poll)."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _headers(self, agent_jwt: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {agent_jwt}",
            "traceparent": trace.current_traceparent(),
            "X-Request-ID": trace.request_id_var.get(),
        }

    async def request_and_wait(
        self,
        ctx: Any,
        *,
        operation_type: str,
        context: dict[str, Any],
    ) -> bool:
        """Request approval for ``operation_type`` and wait for the human verdict (FAIL-CLOSED).

        Returns True only on an explicit (or auto) approval; False on deny/expire/timeout/error.
        This is the gate the tool-loop uses: an ``ask`` action never runs without a grant.
        """
        verdict = await self.request_verdict(ctx, operation_type=operation_type, context=context)
        return verdict is HilVerdict.GRANTED

    async def request_verdict(
        self,
        ctx: Any,
        *,
        operation_type: str,
        context: dict[str, Any],
    ) -> HilVerdict:
        """Request approval and wait, returning the TRI-STATE :class:`HilVerdict`.

        ``DENIED`` is returned ONLY when a human explicitly denied the request. Every other
        non-grant — an Auth transport error, a >=400, a missing ``request_id``, an expiry, or the
        local wait budget elapsing — is ``UNAVAILABLE``: no verdict could be obtained. Fail-closed
        callers collapse both to "not granted" (:meth:`request_and_wait`); callers that must tell
        *refused* from *unreachable* branch on the enum.
        """
        base = self._settings.auth_service_url.rstrip("/")
        agent_jwt = ctx.inbound_agent_jwt
        headers = self._headers(agent_jwt)
        try:
            resp = await self._http().post(
                f"{base}/v1/hil/approvals/request",
                headers=headers,
                json={"operation_type": operation_type, "context": context},
            )
        except httpx.HTTPError as exc:
            logger.warning("hil_request_failed", error=str(exc))
            return HilVerdict.UNAVAILABLE
        if resp.status_code >= 400:
            logger.warning("hil_request_rejected", status=resp.status_code)
            return HilVerdict.UNAVAILABLE
        body = resp.json()
        if body.get("auto_approved"):
            return HilVerdict.GRANTED
        request_id = body.get("request_id")
        if not request_id:
            return HilVerdict.UNAVAILABLE

        # Poll until resolved or the wait budget elapses.
        poll_interval = max(1, self._settings.hil_poll_interval_seconds)
        deadline = asyncio.get_event_loop().time() + self._settings.hil_max_wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            status = await self._poll(base, headers, str(request_id))
            if status == "granted":
                logger.info("hil_approval_granted", request_id=request_id)
                return HilVerdict.GRANTED
            if status == "denied":
                logger.info("hil_approval_resolved", request_id=request_id, status=status)
                return HilVerdict.DENIED
            if status == "expired":
                # Nobody answered in time. That is NOT a refusal — see HilVerdict.UNAVAILABLE.
                logger.info("hil_approval_resolved", request_id=request_id, status=status)
                return HilVerdict.UNAVAILABLE
        logger.info("hil_approval_wait_timeout", request_id=request_id)
        return HilVerdict.UNAVAILABLE

    async def _poll(self, base: str, headers: dict[str, str], request_id: str) -> str:
        try:
            resp = await self._http().get(f"{base}/v1/hil/approvals/{request_id}", headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("hil_poll_failed", request_id=request_id, error=str(exc))
            return "pending"  # transient — keep waiting until the budget elapses
        if resp.status_code >= 400:
            return "pending"
        return str(resp.json().get("status", "pending"))
