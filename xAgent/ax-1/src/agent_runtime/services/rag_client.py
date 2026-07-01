"""RAG-service client (RAG-query stage, WP12).

Calls ``POST /v1/kbs/{kb_id}/query`` on the RAG service and normalises the response into
a :class:`RagResult`. Identity flows via HEADERS only (Contract 13) — the body carries NO
identity:

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent`` + ``tracestate`` + ``X-Request-ID`` (Contract 8 W3C propagation,
    via ``trace.propagation_headers()`` — tracestate flows only when present)

Body: ``{ query, top_k }``. The response is normalised to ``{ kb_id, results, forbidden }``.

FAIL POSTURE — a 403 (KB ACL deny, ``FORBIDDEN_KB``) is NOT an exception: RAG retrieval is
an OPTIONAL enhancement, so a denied KB must degrade gracefully (the stage skips that KB
and continues) rather than failing the whole task. It is surfaced as a typed result with
``forbidden=True`` and an empty ``results`` list. Every OTHER non-2xx (and any transport
error) raises :class:`ApiError` ``SERVICE_UNAVAILABLE`` — the calling stage decides whether
RAG is hard or soft for that agent.
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
class RagChunk:
    """A single retrieved knowledge-base chunk."""

    chunk_id: str
    text: str
    score: float
    document_id: str | None = None


@dataclass
class RagResult:
    """Normalised RAG-query result.

    ``forbidden`` is set when the RAG service returned 403 (KB ACL deny / ``FORBIDDEN_KB``):
    a typed, non-exceptional outcome so the stage can skip the KB and carry on.
    """

    kb_id: str
    results: list[RagChunk] = field(default_factory=list)
    forbidden: bool = False


class RagClient:
    """Thin async client for the RAG knowledge-base query endpoint."""

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
            self._client = httpx.AsyncClient(timeout=self._settings.rag_timeout_seconds)
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

    async def query(
        self,
        kb_id: str,
        query: str,
        top_k: int,
        *,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> RagResult:
        """Query ``kb_id`` for the ``top_k`` chunks most relevant to ``query``.

        Returns a :class:`RagResult`. A 403 KB ACL deny is returned as
        ``RagResult(forbidden=True, results=[])`` (NOT raised). Any other non-2xx or a
        transport error raises ``ApiError`` SERVICE_UNAVAILABLE.
        """
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        body = {"query": query, "top_k": top_k}
        url = f"{self._settings.rag_service_url.rstrip('/')}/v1/kbs/{kb_id}/query"
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("rag", "error").inc()
            logger.warning("rag_query_failed", kb_id=kb_id, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "RAG service unavailable.") from exc

        if resp.status_code == 403:
            # KB ACL deny (FORBIDDEN_KB) — typed, non-exceptional so the stage skips this KB.
            metrics.downstream_calls_total.labels("rag", "forbidden").inc()
            logger.info("rag_query_forbidden_kb", kb_id=kb_id)
            return RagResult(kb_id=kb_id, results=[], forbidden=True)
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("rag", "rejected").inc()
            logger.warning("rag_query_rejected", kb_id=kb_id, status=resp.status_code)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"RAG service returned {resp.status_code}.",
            )

        metrics.downstream_calls_total.labels("rag", "ok").inc()
        return self._parse(kb_id, resp.json())

    @staticmethod
    def _parse(kb_id: str, data: dict[str, Any]) -> RagResult:
        chunks: list[RagChunk] = []
        for item in data.get("results", []) or []:
            chunks.append(
                RagChunk(
                    chunk_id=str(item.get("chunk_id", "")),
                    text=str(item.get("text", "")),
                    score=float(item.get("score", 0.0)),
                    document_id=item.get("document_id"),
                )
            )
        return RagResult(kb_id=kb_id, results=chunks, forbidden=False)
