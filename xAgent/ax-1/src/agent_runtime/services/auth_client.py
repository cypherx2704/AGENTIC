"""Auth-service client — agent-existence cross-validation (Component 1 step 2) +
task-submission authorize check (layer-B, WP08).

The runtime-config endpoint (``POST /v1/agents/{agent_id}/runtime``) must confirm the
target agent actually exists in the Auth identity registry AND that its tenant matches
the caller's JWT tenant BEFORE it writes the ``xagent.agents`` runtime row. This client
calls Auth ``GET /v1/agents/{agent_id}`` using the shared service-token provider.

WP08 adds :meth:`AuthClient.authorize` — a layer-B check that the caller is authorized
for an action (``task:execute``) at Auth ``POST /v1/authorize``, fronted by a
Valkey-cached verdict (60s TTL) so a suspended tenant/agent stops within 60s WITHOUT a
per-task Auth round-trip. FAIL POSTURE (documented, decided): the authorize check
FAILS OPEN — an Auth or Valkey error ACCEPTS the submission (+log +metric). Rationale:
the inbound agent JWT was already cryptographically verified (signature/iss/aud/exp/
scope ``agent:execute`` + the revocation mirror) in ``core.auth`` BEFORE this runs, so
layer-B is defense-in-depth, not the only gate; the 60s cache means a genuine
revocation still propagates fast. A DEFINITIVE Auth ``deny`` is honored (403 FORBIDDEN).

Identity flows via HEADERS only (Contract 13):

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent: <current trace>``                    (Contract 8 propagation)

The response is normalised to :class:`AuthAgent` (``agent_id`` + ``tenant_id``). A 404
from Auth surfaces as :class:`ApiError` ``NOT_FOUND``; any other non-2xx surfaces as
``SERVICE_UNAVAILABLE`` (Auth reachability is a hard dependency at registration time,
but a transient Auth failure must not masquerade as "agent does not exist").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class AuthAgent:
    """Normalised view of an Auth ``GET /v1/agents/{id}`` response."""

    agent_id: str
    tenant_id: str


class AuthClient:
    """Thin async client for the Auth agent-registry read endpoint."""

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
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def get_agent(
        self,
        agent_id: str,
        *,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> AuthAgent:
        """Fetch the Auth identity row for ``agent_id``.

        Raises ``ApiError`` NOT_FOUND when Auth returns 404, SERVICE_UNAVAILABLE on
        network failure or any other non-2xx status.
        """
        # Auth authenticates the CALLER from the Bearer token and has NO X-Forwarded-Agent-JWT
        # handling (unlike llms/guardrails). The caller here is an authenticated admin
        # (register_runtime enforced agent:admin/platform:admin upstream) whose own verified
        # agent JWT already carries the tenant_id + admin scope Auth needs to authorize the read.
        # Forward THAT JWT directly: a Contract-12 service token is tenantless (sub=svc:xagent,
        # no tenant_id claim), so Auth's CallerContext 403s it ("Caller token has no tenant_id
        # claim"). The service-token provider remains the credential for xAgent -> llms/guardrails.
        headers = {
            "Authorization": f"Bearer {agent_jwt}",
            "traceparent": trace.current_traceparent(),
            "X-Request-ID": trace.request_id_var.get(),
        }
        url = f"{self._settings.auth_service_url.rstrip('/')}/v1/agents/{agent_id}"
        try:
            resp = await self._http().get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("auth_get_agent_failed", agent_id=agent_id, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Auth service unavailable.") from exc

        if resp.status_code == 404:
            raise ApiError(ErrorCode.NOT_FOUND, f"Agent {agent_id} not found in Auth registry.")
        if resp.status_code >= 400:
            logger.warning("auth_get_agent_rejected", agent_id=agent_id, status=resp.status_code)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Auth service returned {resp.status_code}.",
            )

        data = resp.json()
        resolved_tenant = data.get("tenant_id")
        if not resolved_tenant:
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Auth returned an agent without a tenant_id.")
        return AuthAgent(agent_id=str(data.get("agent_id", agent_id)), tenant_id=str(resolved_tenant))

    async def authorize(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        action: str,
        agent_jwt: str,
        valkey: Any | None = None,
    ) -> None:
        """Layer-B authorize check for ``action`` (Valkey-cached verdict, 60s TTL).

        Order of operations:
          1. Try the Valkey verdict cache (``<prefix>authz:{tenant}:{agent}:{action}``).
             A cached ``deny`` -> raise FORBIDDEN immediately (a suspension propagates
             within the TTL without re-asking Auth). A cached ``allow`` -> return.
          2. On a cache MISS, call Auth ``POST /v1/authorize`` and cache the verdict.

        FAIL-OPEN: a missing Valkey client, a Valkey error, or an Auth transport/5xx
        error ACCEPTS the submission (+log +metric). Only a DEFINITIVE Auth ``deny``
        (or 403) raises FORBIDDEN. ``valkey`` is the real :class:`ValkeyClient` when
        configured, the test double (no ``get_authorize_verdict`` method) or ``None``
        otherwise — both of the latter skip the cache and read straight through to Auth.
        """
        s = self._settings
        if not s.authorize_enabled:
            metrics.authorize_checks_total.labels("disabled").inc()
            return

        cache_capable = valkey is not None and hasattr(valkey, "get_authorize_verdict")

        # 1) Cache read (fail-open on any cache error).
        if cache_capable:
            try:
                cached = await valkey.get_authorize_verdict(
                    prefix=s.task_signal_key_prefix,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    action=action,
                    timeout_seconds=s.task_signal_valkey_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 — Valkey down/slow: fall through to Auth
                logger.warning("authorize_cache_read_failed", error=str(exc))
                cached = None
            if cached is True:
                metrics.authorize_checks_total.labels("cache_hit").inc()
                return
            if cached is False:
                metrics.authorize_checks_total.labels("deny").inc()
                logger.info("authorize_denied_cached", tenant_id=tenant_id, agent_id=agent_id)
                raise ApiError(ErrorCode.FORBIDDEN, f"Not authorized for action {action!r}.")

        # 2) Cache miss -> ask Auth (fail-open on transport/5xx; honor a definitive deny).
        allowed = await self._authorize_remote(
            tenant_id=tenant_id, agent_id=agent_id, action=action, agent_jwt=agent_jwt
        )

        if cache_capable:
            await valkey.set_authorize_verdict(
                prefix=s.task_signal_key_prefix,
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=action,
                allowed=allowed,
                ttl_seconds=s.authorize_cache_ttl_seconds,
                timeout_seconds=s.task_signal_valkey_timeout_seconds,
            )

        if not allowed:
            metrics.authorize_checks_total.labels("deny").inc()
            logger.info("authorize_denied", tenant_id=tenant_id, agent_id=agent_id, action=action)
            raise ApiError(ErrorCode.FORBIDDEN, f"Not authorized for action {action!r}.")
        metrics.authorize_checks_total.labels("allow").inc()

    async def _authorize_remote(
        self, *, tenant_id: str, agent_id: str, action: str, agent_jwt: str
    ) -> bool:
        """Call Auth ``POST /v1/authorize``; return the decision. FAIL-OPEN to True.

        A 403 (or a 200 body with ``allowed: false`` / ``decision: 'deny'``) is a
        DEFINITIVE deny -> False. A transport error or any other non-2xx is treated as
        allow (availability wins; the JWT was already verified upstream).
        """
        # Contract-13: identity (tenant_id/agent_id) comes ONLY from the forwarded agent JWT — Auth
        # rejects 400 if they are asserted in the body. Send the action only.
        body = {"action": action}
        url = f"{self._settings.auth_service_url.rstrip('/')}/v1/authorize"
        try:
            # Contract-12: authenticate to Auth with the xAgent SERVICE token (sub=svc:xagent) and
            # forward the inbound agent JWT verbatim. /v1/authorize requires BOTH — a bare agent JWT
            # as the caller credential is rejected 401 (which previously made every call fail-open).
            # Mirrors the llms/guardrails clients' header pattern.
            service_jwt = await self._tokens.get_token(on_behalf_of=agent_id)
            headers = {
                "Authorization": f"Bearer {service_jwt}",
                "X-Forwarded-Agent-JWT": agent_jwt,
                "traceparent": trace.current_traceparent(),
                "X-Request-ID": trace.request_id_var.get(),
            }
            resp = await self._http().post(
                url, headers=headers, json=body, timeout=self._settings.authorize_timeout_seconds
            )
        except (httpx.HTTPError, ApiError) as exc:
            metrics.authorize_checks_total.labels("fail_open").inc()
            logger.warning("authorize_call_failed_fail_open", error=str(exc))
            return True
        if resp.status_code == 403:
            return False
        if resp.status_code >= 400:
            metrics.authorize_checks_total.labels("fail_open").inc()
            logger.warning("authorize_call_rejected_fail_open", status=resp.status_code)
            return True
        return self._verdict_from_body(resp.json())

    @staticmethod
    def _verdict_from_body(data: dict[str, Any]) -> bool:
        """Interpret an Auth 200 authorize body as allow/deny (defaults to allow).

        Accepts the common shapes: ``{"allowed": bool}`` or ``{"decision": "allow"|"deny"}``.
        A body without a recognizable verdict defaults to allow (fail-open posture).
        """
        if "allowed" in data:
            return bool(data["allowed"])
        decision = data.get("decision")
        if decision is not None:
            return str(decision).lower() not in ("deny", "denied", "block", "blocked")
        return True
