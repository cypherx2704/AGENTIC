"""Memory-service client — the copilot's CONVERSATIONAL working memory ONLY.

Per-principal episodic context across chat turns (prior questions, session continuity).
This is NOT where the engineering knowledge corpus lives (that is the graph + RAG) — see
the lint guard in tests and docs/02-sharedcore-integration-boundary.md. Identity via HEADERS
only (Contract 12 + forwarded agent JWT + trace).

The copilot treats memory as best-effort: a store/search failure NEVER fails an answer
(availability over completeness), so reads return ``[]`` and writes swallow errors on a
downstream outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class MemoryItem:
    content: str
    type: str
    similarity: float | None = None


class MemoryClient:
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
            self._client = httpx.AsyncClient(timeout=self._settings.memory_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _headers(
        self, *, agent_jwt: str, on_behalf_of: str | None, idempotency_key: str | None = None
    ) -> dict[str, str]:
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "X-Forwarded-Agent-JWT": agent_jwt,
            **trace.propagation_headers(),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def search(
        self, *, query: str, top_k: int, agent_jwt: str, on_behalf_of: str | None = None
    ) -> list[MemoryItem]:
        """Best-effort recall of prior conversational context. Returns [] on any failure."""
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body = {"query": query, "top_k": top_k, "include_shared": False}
        url = f"{self._settings.memory_service_url.rstrip('/')}/v1/memories/search"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
            if resp.status_code >= 400:
                metrics.downstream_calls_total.labels("memory", "rejected").inc()
                return []
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("memory", "error").inc()
            logger.info("memory_search_skipped", error=str(exc))
            return []
        metrics.downstream_calls_total.labels("memory", "ok").inc()
        data = resp.json()
        return [
            MemoryItem(
                content=str(r.get("content", "")),
                type=str(r.get("type", "episodic")),
                similarity=r.get("similarity"),
            )
            for r in data.get("results", []) or []
        ]

    async def store(
        self,
        *,
        content: str,
        memory_type: str,
        session_id: str | None,
        agent_jwt: str,
        on_behalf_of: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Store an episodic memory (principal_only). Best-effort; returns False on failure."""
        headers = await self._headers(
            agent_jwt=agent_jwt, on_behalf_of=on_behalf_of, idempotency_key=idempotency_key
        )
        body: dict[str, Any] = {
            "content": content,
            "type": memory_type,
            "scope": "principal_only",
        }
        if session_id:
            body["session_id"] = session_id
        url = f"{self._settings.memory_service_url.rstrip('/')}/v1/memories"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("memory", "error").inc()
            logger.info("memory_store_skipped", error=str(exc))
            return False
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("memory", "rejected").inc()
            return False
        metrics.downstream_calls_total.labels("memory", "ok").inc()
        return True

    async def ensure_session(
        self, *, session_id: str, agent_jwt: str, on_behalf_of: str | None = None
    ) -> None:
        """Register a session (idempotent for the same principal). Best-effort — a 409
        (already exists) or any transport error is non-fatal for the copilot, so this never
        raises."""
        try:
            headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
            body = {"session_id": session_id}
            url = f"{self._settings.memory_service_url.rstrip('/')}/v1/sessions"
            await self._http().post(url, headers=headers, json=body)
        except Exception as exc:  # noqa: BLE001 — session registration is best-effort
            logger.info("memory_ensure_session_skipped", error=str(exc))
