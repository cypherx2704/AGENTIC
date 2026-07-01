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
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from ..core import trace
from ..core.config import Settings

logger = structlog.get_logger(__name__)


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
        """Request approval for ``operation_type`` and wait for the human verdict.

        Returns True only on an explicit (or auto) approval; False on deny/expire/timeout/error.
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
            return False
        if resp.status_code >= 400:
            logger.warning("hil_request_rejected", status=resp.status_code)
            return False
        body = resp.json()
        if body.get("auto_approved"):
            return True
        request_id = body.get("request_id")
        if not request_id:
            return False

        # Poll until resolved or the wait budget elapses.
        poll_interval = max(1, self._settings.hil_poll_interval_seconds)
        deadline = asyncio.get_event_loop().time() + self._settings.hil_max_wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            status = await self._poll(base, headers, str(request_id))
            if status == "granted":
                logger.info("hil_approval_granted", request_id=request_id)
                return True
            if status in ("denied", "expired"):
                logger.info("hil_approval_resolved", request_id=request_id, status=status)
                return False
        logger.info("hil_approval_wait_timeout", request_id=request_id)
        return False

    async def _poll(self, base: str, headers: dict[str, str], request_id: str) -> str:
        try:
            resp = await self._http().get(f"{base}/v1/hil/approvals/{request_id}", headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("hil_poll_failed", request_id=request_id, error=str(exc))
            return "pending"  # transient — keep waiting until the budget elapses
        if resp.status_code >= 400:
            return "pending"
        return str(resp.json().get("status", "pending"))
