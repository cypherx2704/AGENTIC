"""Rerank provider — llms-gateway ``POST /v1/rerank`` with a MOCK fallback.

Mirrors the embeddings client (``services/embeddings.py``) exactly: a Contract-12 service
JWT (minted via the ServiceTokenProvider) + ``X-Forwarded-Agent-JWT`` (the originating
agent), forwarding ``X-Request-ID`` / ``traceparent``. Mock-tolerant so keyless local dev +
the test suite never need a live gateway.

Behaviour (only reached when ``RAG_RERANK_ENABLED`` is on AND a query opts in with
``rerank=true``):
  * ``mock_rerank`` (or ``mock_embeddings``) is true -> deterministic in-process reranker.
  * otherwise -> call the gateway; on failure, if ``rerank_fallback_to_base`` is true the
    BASE ordering is returned unchanged (the request never 5xxs because of rerank); else the
    error propagates.

The gateway contract is additive + additionalProperties-tolerant: we send
``{model, query, documents:[...], top_n}`` and read back ``{results:[{index, relevance_score}]}``
(also tolerating ``data`` / ``score``). Unknown fields are ignored.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx
import structlog

from ..core import trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class RerankItem:
    """One reranked result: the original list index + its relevance score."""

    index: int
    relevance_score: float


@dataclass
class RerankResult:
    items: list[RerankItem]  # ordered best-first, length == min(top_n, len(documents))
    model: str
    source: str  # 'mock' | 'llms' | 'fallback_base'


def _mock_score(query: str, document: str) -> float:
    """Deterministic, query-aware pseudo relevance in [0, 1].

    Lexical overlap of query tokens with the document (Jaccard-ish), nudged by a stable hash
    so ties break deterministically. Good enough to make the eval harness + tests observe a
    *query-relevant* reordering without any network. Byte-stable across runs/processes.
    """
    q_tokens = {t for t in query.lower().split() if t}
    d_tokens = {t for t in document.lower().split() if t}
    overlap = len(q_tokens & d_tokens)
    denom = len(q_tokens) or 1
    base = overlap / denom
    # Small deterministic jitter in [0, 0.01) for stable tie-breaking.
    h = int(hashlib.sha256(f"{query}|{document}".encode()).hexdigest()[:8], 16)
    jitter = (h % 1000) / 100000.0
    return min(1.0, base + jitter)


def mock_rerank(query: str, documents: list[str], top_n: int) -> list[RerankItem]:
    """Public deterministic reranker (used by the mock path + tests + eval harness)."""
    scored = [RerankItem(index=i, relevance_score=_mock_score(query, d)) for i, d in enumerate(documents)]
    scored.sort(key=lambda it: (it.relevance_score, -it.index), reverse=True)
    return scored[: max(0, top_n)]


class RerankClient:
    """Reranks candidate documents via the llms-gateway with a deterministic mock fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        token_provider: ServiceTokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.llms_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _is_mock(self) -> bool:
        return self._settings.mock_rerank or self._settings.mock_embeddings

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str | None = None,
        agent_jwt: str | None = None,
        on_behalf_of: str | None = None,
    ) -> RerankResult:
        """Rerank ``documents`` against ``query``; returns the best ``top_n`` (index + score)."""
        model = model or self._settings.rerank_model
        if not documents:
            return RerankResult(items=[], model=model, source="mock" if self._is_mock() else "llms")

        if self._is_mock():
            return RerankResult(items=mock_rerank(query, documents, top_n), model=model, source="mock")

        try:
            return await self._rerank_via_llms(
                query, documents, top_n=top_n, model=model,
                agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to the base ordering if configured
            if self._settings.rerank_fallback_to_base:
                logger.warning("rerank_fallback_to_base", error=str(exc))
                base = [RerankItem(index=i, relevance_score=0.0) for i in range(min(top_n, len(documents)))]
                return RerankResult(items=base, model=model, source="fallback_base")
            if isinstance(exc, ApiError):
                raise
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Rerank provider unavailable.") from exc

    async def _rerank_via_llms(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str,
        agent_jwt: str | None,
        on_behalf_of: str | None,
    ) -> RerankResult:
        if self._tokens is None:
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "No service-token provider configured.")
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "traceparent": trace.current_traceparent(),
            "X-Request-ID": trace.request_id_var.get(),
        }
        if agent_jwt:
            headers["X-Forwarded-Agent-JWT"] = agent_jwt

        body = {"model": model, "query": query, "documents": documents, "top_n": top_n}
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/rerank"
        resp = await self._http().post(url, headers=headers, json=body)
        if resp.status_code == 429:
            raise ApiError(
                ErrorCode.RATE_LIMIT_EXCEEDED,
                "Rerank provider rate limited.",
                headers={"Retry-After": resp.headers.get("Retry-After", "2")},
            )
        if resp.status_code >= 400:
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Rerank provider returned {resp.status_code}.",
            )
        data = resp.json()
        rows = data.get("results", data.get("data", [])) if isinstance(data, dict) else data
        items: list[RerankItem] = []
        for r in rows:
            if not isinstance(r, dict) or "index" not in r:
                continue
            score = r.get("relevance_score", r.get("score", 0.0))
            items.append(RerankItem(index=int(r["index"]), relevance_score=float(score)))
        # Gateway should already sort, but enforce best-first + the top_n cap defensively.
        items.sort(key=lambda it: it.relevance_score, reverse=True)
        return RerankResult(items=items[:top_n], model=data.get("model", model), source="llms")
