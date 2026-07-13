"""Memory-service client (memory-retrieve + memory-write stages, WP12).

Calls ``POST /v1/memories/search`` (retrieve) and ``POST /v1/memories`` (store) on the
Memory service. Identity flows via HEADERS only (Contract 13) — the body carries NO
identity:

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent`` + ``tracestate`` + ``X-Request-ID`` (Contract 8 W3C propagation,
    via ``trace.propagation_headers()`` — tracestate flows only when present)

The ``type``/``tags``/``scope``/``session_id``/``metadata`` selectors ARE part of the
memory query/record (they scope WHICH memories, not WHO the caller is), so they travel in
the body — distinct from identity. Both calls raise :class:`ApiError`
``SERVICE_UNAVAILABLE`` on a non-2xx / transport error: the calling stage decides whether
memory is hard or soft for that agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .errors import error_detail as _error_detail
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


# xAgent's agent-config ``memory_scope`` vocabulary (none/agent/user/tenant/session) is a
# DIFFERENT model from the Memory service's wire API (the built WP10 service uses a 2-value
# ``scope`` = principal_only|tenant_shared on STORE, and ``include_shared`` + ``session_scope_id``
# on SEARCH — there is NO ``scope`` field on SearchMemoryRequest). These helpers translate the
# agent-level scope into the memory service's actual fields. "tenant" shares across principals;
# everything else stays principal-private; "session" additionally narrows by session_scope_id.
def _store_scope(agent_scope: str | None) -> str:
    """Map agent memory_scope -> Memory StoreMemoryRequest.scope (principal_only|tenant_shared)."""
    return "tenant_shared" if agent_scope == "tenant" else "principal_only"


@dataclass
class MemoryItem:
    """A single retrieved memory record (extra fields preserved in ``raw``)."""

    id: str
    content: str
    score: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemorySearchResult:
    """Normalised memory-search response."""

    results: list[MemoryItem] = field(default_factory=list)


@dataclass
class MemoryStoreResult:
    """Outcome of a store (POST /v1/memories) — the new record's id when returned."""

    id: str | None = None


class MemoryClient:
    """Thin async client for the Memory search + store endpoints."""

    def __init__(
        self,
        settings: Settings,
        token_provider: ServiceTokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client  # injectable for tests (respx); lazily created otherwise
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.memory_timeout_seconds)
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

    async def search(
        self,
        query: str,
        top_k: int,
        *,
        agent_jwt: str,
        type: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        session_id: str | None = None,
        on_behalf_of: str | None = None,
    ) -> MemorySearchResult:
        """Search memories relevant to ``query``.

        ``type``/``tags``/``scope``/``session_id`` are OPTIONAL body selectors (omitted
        from the request when ``None``). Raises ``ApiError`` SERVICE_UNAVAILABLE on a
        non-2xx / transport error.
        """
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if type is not None:
            body["type"] = type
        if tags is not None:
            body["tags"] = tags
        # Map agent memory_scope -> Memory SearchMemoryRequest fields (it has NO ``scope`` field):
        # "tenant" includes cross-principal tenant_shared rows; everything else stays own-principal.
        if scope is not None:
            body["include_shared"] = scope == "tenant"
        # The Memory search API scopes a session via ``session_scope_id`` (not ``session_id``).
        if session_id is not None:
            body["session_scope_id"] = session_id
        url = f"{self._settings.memory_service_url.rstrip('/')}/v1/memories/search"
        data = await self._call("search", url, body, headers)
        items: list[MemoryItem] = []
        for item in data.get("results", []) or []:
            items.append(
                MemoryItem(
                    id=str(item.get("id", "")),
                    content=str(item.get("content", "")),
                    score=float(item.get("score", 0.0)),
                    raw=item,
                )
            )
        return MemorySearchResult(results=items)

    async def store(
        self,
        content: str,
        *,
        agent_jwt: str,
        type: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        on_behalf_of: str | None = None,
    ) -> MemoryStoreResult:
        """Store a memory record (expects 201). Returns the new id when the body carries it.

        ``type``/``tags``/``scope``/``session_id``/``metadata`` are OPTIONAL body fields
        (omitted when ``None``). Raises ``ApiError`` SERVICE_UNAVAILABLE on a non-2xx /
        transport error.
        """
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body: dict[str, Any] = {"content": content}
        if type is not None:
            body["type"] = type
        if tags is not None:
            body["tags"] = tags
        # Map agent memory_scope -> Memory StoreMemoryRequest.scope (principal_only|tenant_shared).
        if scope is not None:
            body["scope"] = _store_scope(scope)
        # ``session_id`` IS a valid store field; for session scope also narrow via session_scope_id.
        if session_id is not None:
            body["session_id"] = session_id
            if scope == "session":
                body["session_scope_id"] = session_id
        if metadata is not None:
            body["metadata"] = metadata
        url = f"{self._settings.memory_service_url.rstrip('/')}/v1/memories"
        data = await self._call("store", url, body, headers)
        return MemoryStoreResult(id=data.get("id") if isinstance(data, dict) else None)

    async def _call(
        self, op: str, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("memory", "error").inc()
            logger.warning("memory_call_failed", op=op, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Memory service unavailable.") from exc
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("memory", "rejected").inc()
            detail = _error_detail(resp)  # Contract-2 message — the whole diagnosis
            logger.warning("memory_call_rejected", op=op, status=resp.status_code, detail=detail)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Memory service returned {resp.status_code}." + (f" {detail}" if detail else ""),
            )
        metrics.downstream_calls_total.labels("memory", "ok").inc()
        # A 201 store may legitimately carry an empty body — tolerate non-JSON / empty.
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
