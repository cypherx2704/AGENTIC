"""Tool Registry client (INTERNAL / Contract-12 auth).

Registers a published workflow as a Contract-4 MCP tool in the existing Tool Registry and
governs its access. Every call authenticates as the bridge SERVICE principal (short-lived
service JWT via :class:`ServiceTokenProvider`) while forwarding the publishing user's agent
JWT as ``X-Forwarded-Agent-JWT`` — the registry takes tenant_id + ``tool:admin`` /
``tenant:admin`` from that forwarded user JWT (Contract 13), never from the service token.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..core import metrics
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


class RegistryClient:
    def __init__(
        self,
        settings: Settings,
        token_provider: ServiceTokenProvider,
        client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client

    @property
    def _base(self) -> str:
        return self._settings.tool_registry_url.rstrip("/")

    async def _headers(
        self, *, user_jwt: str, agent_id: str, trace_headers: dict[str, str] | None
    ) -> dict[str, str]:
        svc_token = await self._tokens.get_token(on_behalf_of=agent_id)
        headers = {
            "Authorization": f"Bearer {svc_token}",
            "X-Forwarded-Agent-JWT": user_jwt,
            "content-type": "application/json",
        }
        if trace_headers:
            headers.update(trace_headers)
        return headers

    async def register(
        self,
        *,
        user_jwt: str,
        agent_id: str,
        name: str,
        manifest: dict[str, Any],
        is_update: bool,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create the tool (first publish) or append a version (re-publish)."""
        headers = await self._headers(user_jwt=user_jwt, agent_id=agent_id, trace_headers=trace_headers)
        if is_update:
            return await self._post_version(name, manifest, headers)
        resp = await self._post(f"{self._base}/v1/tools", manifest, headers)
        if resp.status_code == 201:
            metrics.registry_call_total.labels("register", "ok").inc()
            return resp.json()
        if resp.status_code == 409:
            # Already registered (binding drifted) — append a version instead.
            return await self._post_version(name, manifest, headers)
        self._raise(resp, "register")

    async def _post_version(
        self, name: str, manifest: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        resp = await self._post(f"{self._base}/v1/tools/{name}/versions", manifest, headers)
        if resp.status_code in (200, 201):
            metrics.registry_call_total.labels("version", "ok").inc()
            return resp.json()
        if resp.status_code == 404:
            # No existing tool to version — create it fresh.
            create = await self._post(f"{self._base}/v1/tools", manifest, headers)
            if create.status_code == 201:
                metrics.registry_call_total.labels("register", "ok").inc()
                return create.json()
            self._raise(create, "register")
        self._raise(resp, "version")

    async def mark_restricted(
        self,
        *,
        user_jwt: str,
        agent_id: str,
        name: str,
        reason: str,
        default_access_mode: str = "none",
        trace_headers: dict[str, str] | None = None,
    ) -> None:
        """Mark the tool restricted and set its server-wide default access mode. ``ask`` makes
        the tool callable by every tenant agent subject to HIL approval (the flow-tool default);
        ``none`` is default-deny until a tenant admin grants a specific agent. Requires the
        user JWT to carry ``tenant:admin``."""
        headers = await self._headers(user_jwt=user_jwt, agent_id=agent_id, trace_headers=trace_headers)
        resp = await self._post(
            f"{self._base}/v1/restricted-tools/{name}",
            {"reason": reason, "default_access_mode": default_access_mode},
            headers,
        )
        if resp.status_code in (200, 201, 409):
            metrics.registry_call_total.labels("restrict", "ok").inc()
            return
        self._raise(resp, "restrict")

    async def _post(
        self, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        try:
            return await self._client.post(
                url, json=body, headers=headers, timeout=self._settings.registry_timeout_seconds
            )
        except httpx.HTTPError as exc:
            metrics.registry_call_total.labels("post", "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, f"Tool Registry unreachable: {exc}"
            ) from exc

    def _raise(self, resp: httpx.Response, op: str) -> None:
        metrics.registry_call_total.labels(op, "error").inc()
        message = f"Tool Registry {op} failed ({resp.status_code})."
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                message = body["error"].get("message", message)
        except ValueError:
            pass
        if resp.status_code == 403:
            raise ApiError(ErrorCode.FORBIDDEN, message)
        if resp.status_code == 401:
            raise ApiError(ErrorCode.UNAUTHORIZED, message)
        if resp.status_code == 409:
            raise ApiError(ErrorCode.CONFLICT, message)
        if resp.status_code == 400 or resp.status_code == 422:
            raise ApiError(ErrorCode.VALIDATION_ERROR, message, status_code=422)
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, message)
