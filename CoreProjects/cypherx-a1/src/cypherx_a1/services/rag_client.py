"""RAG-service client — the engineering VECTOR/SEMANTIC corpus (delegated storage).

cypherx-a1 leases RAG knowledge bases for its dense-retrieval leg: it creates a small fixed
set of per-tenant KBs (with an EXPLICIT pinned embedding model — never the repointable
'embed' alias), ingests normalized engineering text inline (≤100 KiB), and queries
``POST /v1/kbs/{kb_id}/query`` for the dense hits. Rich provenance lives in
``chunk.metadata`` so the orchestrator can map a hit back to its graph entity for a
citation. Identity via HEADERS only (Contract 12 + forwarded agent JWT + trace).

The GRAPH never enters RAG (rag.chunks are opaque text+JSONB). Hybrid fusion, keyword,
rerank, query expansion, and range/time filtering all stay app-side (RAG is dense-only +
``@>``-containment filters first cycle).
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
class KbInfo:
    kb_id: str
    embedding_model_resolved: str
    embedding_dim: int


@dataclass
class IngestResult:
    doc_id: str
    status: str


@dataclass
class RagHit:
    chunk_id: str
    doc_id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    source_name: str | None = None
    source_uri: str | None = None


@dataclass
class RagQueryResult:
    kb_id: str
    results: list[RagHit] = field(default_factory=list)
    forbidden: bool = False


class RagClient:
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
            self._client = httpx.AsyncClient(timeout=self._settings.rag_timeout_seconds)
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

    async def create_kb(
        self, *, name: str, agent_jwt: str, on_behalf_of: str | None = None
    ) -> KbInfo:
        """Create a KB with the pinned embedding model (Contract: model resolved + immutable
        at creation). The explicit model name is passed as the alias so RAG resolves it to a
        stable literal rather than the repointable 'embed' default."""
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body = {
            "name": name,
            "description": f"cypherx-a1 engineering memory KB: {name}",
            "chunking_strategy": "sentence",
            "embedding_model_alias": self._settings.rag_embedding_model,
            "private": False,
        }
        url = f"{self._settings.rag_service_url.rstrip('/')}/v1/kbs"
        resp = await self._post(url, headers, body, op="rag_create_kb")
        data = resp.json()
        return KbInfo(
            kb_id=str(data["kb_id"]),
            embedding_model_resolved=str(data.get("embedding_model_resolved", "")),
            embedding_dim=int(data.get("embedding_dim", self._settings.rag_embedding_dim)),
        )

    async def ingest_inline(
        self,
        *,
        kb_id: str,
        name: str,
        content: str,
        source_type: str,
        metadata: dict[str, Any],
        agent_jwt: str,
        on_behalf_of: str | None = None,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        """Inline-ingest one document (≤100 KiB). Synchronous; RAG chunks + embeds it."""
        headers = await self._headers(
            agent_jwt=agent_jwt, on_behalf_of=on_behalf_of, idempotency_key=idempotency_key
        )
        body = {
            "name": name[:500],
            "content": content,
            "source_type": source_type if source_type in ("markdown", "text") else "text",
            "metadata": metadata,
        }
        url = f"{self._settings.rag_service_url.rstrip('/')}/v1/kbs/{kb_id}/documents"
        resp = await self._post(url, headers, body, op="rag_ingest")
        data = resp.json()
        return IngestResult(doc_id=str(data["doc_id"]), status=str(data.get("status", "pending")))

    async def query(
        self,
        *,
        kb_id: str,
        query: str,
        top_k: int,
        agent_jwt: str,
        on_behalf_of: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> RagQueryResult:
        """Dense retrieval. A 403 KB ACL deny is returned as ``forbidden=True`` (NOT raised)
        so the orchestrator can degrade gracefully; any other non-2xx raises."""
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body: dict[str, Any] = {
            "query": query,
            "top_k": min(top_k, 100),
            "min_score": self._settings.rag_query_min_score,
            "search_mode": "dense",
            "ef_search": min(self._settings.rag_query_ef_search, 500),
        }
        if filters:
            body["filters"] = filters
        url = f"{self._settings.rag_service_url.rstrip('/')}/v1/kbs/{kb_id}/query"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("rag", "error").inc()
            logger.warning("rag_query_failed", kb_id=kb_id, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "RAG service unavailable.") from exc
        if resp.status_code == 403:
            metrics.downstream_calls_total.labels("rag", "forbidden").inc()
            return RagQueryResult(kb_id=kb_id, results=[], forbidden=True)
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("rag", "rejected").inc()
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"RAG returned {resp.status_code}.")
        metrics.downstream_calls_total.labels("rag", "ok").inc()
        return self._parse_query(kb_id, resp.json())

    async def _post(
        self, url: str, headers: dict[str, str], body: dict[str, Any], *, op: str
    ) -> httpx.Response:
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("rag", "error").inc()
            logger.warning(f"{op}_failed", error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "RAG service unavailable.") from exc
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("rag", "rejected").inc()
            logger.warning(f"{op}_rejected", status=resp.status_code, body=resp.text[:500])
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"RAG returned {resp.status_code}.")
        metrics.downstream_calls_total.labels("rag", "ok").inc()
        return resp

    @staticmethod
    def _parse_query(kb_id: str, data: dict[str, Any]) -> RagQueryResult:
        hits: list[RagHit] = []
        for item in data.get("results", []) or []:
            src = item.get("source") or {}
            hits.append(
                RagHit(
                    chunk_id=str(item.get("chunk_id", "")),
                    doc_id=str(item.get("doc_id", "")),
                    content=str(item.get("content", "")),
                    score=float(item.get("score", 0.0)),
                    metadata=item.get("metadata") or {},
                    source_name=src.get("name"),
                    source_uri=src.get("uri"),
                )
            )
        return RagQueryResult(kb_id=kb_id, results=hits, forbidden=False)
