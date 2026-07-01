"""Skill-Registry client (SKILL_LOAD stage).

Resolves a skill to its manifest + the calling agent's access mode via the Skill Registry
(the `skills`-schema mirror of the Tool Registry):

  * ``GET /v1/skills/{name}?version=<v>``  -> resolve one skill's manifest/description.
  * ``GET /v1/skills/{name}/access``       -> the agent's mode (none|ask|automated).

Identity flows via HEADERS only (Contract 13), exactly like the Tool-Registry client:

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent`` + ``tracestate`` + ``X-Request-ID`` (Contract 8 W3C propagation)

Skills are declarative (not invoked over MCP like tools), so this client is lean — no
ETag manifest cache. ``resolve_skill`` is FAIL-SOFT (a registry blip lets the SKILL_LOAD
stage skip a skill rather than fail the task); ``get_skill_access`` is FAIL-CLOSED
(any error returns ``none`` — a skill whose access can't be confirmed is not offered).
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
class SkillResolution:
    """A resolved skill — its manifest (description/instructions) + declared scopes."""

    name: str
    version: str
    manifest: dict[str, Any]
    invoke_url: str = ""
    required_scopes: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        return str(self.manifest.get("description", "") or "")


class SkillRegistryClient:
    """Thin async client for the Skill-Registry resolve + access endpoints."""

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
            self._client = httpx.AsyncClient(timeout=self._settings.skill_registry_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _headers(
        self, *, agent_jwt: str, on_behalf_of: str | None, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "X-Forwarded-Agent-JWT": agent_jwt,
            **trace.propagation_headers(),
        }
        if extra:
            headers.update(extra)
        return headers

    async def resolve_skill(
        self,
        name: str,
        version: str | None = None,
        *,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> SkillResolution:
        """Resolve ``name`` (optionally version-pinned) to a :class:`SkillResolution`.

        Raises NOT_FOUND on a 404 and SERVICE_UNAVAILABLE on a transport error / other
        non-2xx — the SKILL_LOAD stage treats both as "skip this skill" (fail-soft).
        """
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        params = {"version": version} if version else None
        url = f"{self._settings.skill_registry_url.rstrip('/')}/v1/skills/{name}"
        try:
            resp = await self._http().get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("skill_registry", "error").inc()
            logger.warning("skill_resolve_failed", name=name, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Skill registry unavailable.") from exc
        if resp.status_code == 404:
            metrics.downstream_calls_total.labels("skill_registry", "rejected").inc()
            raise ApiError(ErrorCode.NOT_FOUND, f"Skill {name!r} not found in registry.")
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("skill_registry", "rejected").inc()
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"Skill registry returned {resp.status_code}.")
        metrics.downstream_calls_total.labels("skill_registry", "ok").inc()
        return self._parse(resp.json())

    async def get_skill_access(
        self,
        name: str,
        *,
        capability: str | None = None,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> str:
        """Resolve the agent's effective access mode for a skill: none|ask|automated.

        FAIL-CLOSED: any error or non-200 returns ``none`` (a skill whose access can't be
        confirmed is not offered to the model), mirroring the Tool-Registry access gate.
        """
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        params = {"capability": capability} if capability else None
        url = f"{self._settings.skill_registry_url.rstrip('/')}/v1/skills/{name}/access"
        try:
            resp = await self._http().get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            logger.warning("skill_access_lookup_failed", name=name, error=str(exc))
            return "none"
        if resp.status_code != 200:
            logger.warning("skill_access_lookup_rejected", name=name, status=resp.status_code)
            return "none"
        try:
            mode = str(resp.json().get("access_mode", "none"))
        except Exception:  # noqa: BLE001 — malformed body -> deny
            return "none"
        return mode if mode in ("none", "ask", "automated") else "none"

    @staticmethod
    def _parse(data: dict[str, Any]) -> SkillResolution:
        return SkillResolution(
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            manifest=data.get("manifest", {}) or {},
            invoke_url=str(data.get("invoke_url", "")),
            required_scopes=list(data.get("required_scopes", []) or []),
        )
