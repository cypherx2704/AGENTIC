"""Embedding provider — llms-gateway ``POST /v1/embeddings`` with a MOCK fallback.

The single embedding entrypoint for the whole service (KB-creation alias resolution,
query embedding, worker batch embedding). Behaviour:

* ``mock_embeddings=true`` (tests + offline local) -> always the deterministic
  in-process mock vector. No network, no service token.
* otherwise -> call the llms-gateway with a Contract-12 service JWT (minted via the
  ServiceTokenProvider) + ``X-Forwarded-Agent-JWT`` (the originating agent), forwarding
  ``X-Request-ID`` / ``traceparent`` and the deterministic per-batch ``Idempotency-Key``.
* on a real-call failure, if ``embeddings_fallback_to_mock=true`` (default) the mock
  vector is returned so query/ingest stay resilient when llms is down locally; the
  metric records the fallback. Production can flip the fallback off to hard-fail.

The mock vector algorithm matches the llms-gateway mock provider EXACTLY (SHA-256 seeded,
L2-normalized, 6-decimal-rounded) so a query embedded by RAG's mock and a chunk embedded
by the gateway's mock land in the same vector space — determinism the tests assert on.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


def _pseudo_vector(text: str, dim: int) -> list[float]:
    """Deterministic L2-normalized pseudo-embedding of ``dim`` floats for ``text``.

    Byte-for-byte identical to the llms-gateway mock provider so the two never drift.
    """
    raw: list[float] = []
    counter = 0
    while len(raw) < dim:
        digest = hashlib.sha256(f"{text}|{counter}".encode()).digest()
        for b in digest:
            raw.append((b / 127.5) - 1.0)  # byte 0..255 -> [-1, 1]
            if len(raw) >= dim:
                break
        counter += 1
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [round(v / norm, 6) for v in raw]


def mock_embed(texts: list[str], dim: int) -> list[list[float]]:
    """Return one deterministic vector per input text (public — used by tests too)."""
    return [_pseudo_vector(t, dim) for t in texts]


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    prompt_tokens: int
    source: str  # 'mock' | 'llms' | 'fallback_mock'


class EmbeddingClient:
    """Embeds text via the llms-gateway with a deterministic mock fallback."""

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

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        dim: int | None = None,
        agent_jwt: str | None = None,
        on_behalf_of: str | None = None,
        idempotency_key: str | None = None,
    ) -> EmbeddingResult:
        """Embed ``texts``. Returns one vector per input string in order.

        ``model`` / ``dim`` default to the configured resolved model + dim. When the
        service is in mock mode (or the real call fails and fallback is on) the
        deterministic mock vectors are returned.
        """
        model = model or self._settings.embedding_model_resolved
        dim = dim or self._settings.embedding_dim

        if self._settings.mock_embeddings:
            metrics.embeddings_total.labels("mock").inc()
            return EmbeddingResult(
                vectors=mock_embed(texts, dim),
                model=model,
                prompt_tokens=_estimate_tokens(texts),
                source="mock",
            )

        try:
            return await self._embed_via_llms(
                texts,
                model=model,
                dim=dim,
                agent_jwt=agent_jwt,
                on_behalf_of=on_behalf_of,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to mock if configured
            if self._settings.embeddings_fallback_to_mock:
                logger.warning("embeddings_fallback_to_mock", error=str(exc))
                metrics.embeddings_total.labels("fallback_mock").inc()
                return EmbeddingResult(
                    vectors=mock_embed(texts, dim),
                    model=model,
                    prompt_tokens=_estimate_tokens(texts),
                    source="fallback_mock",
                )
            if isinstance(exc, ApiError):
                raise
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Embedding provider unavailable.") from exc

    async def resolve_model(
        self, alias: str, *, on_behalf_of: str | None = None, agent_jwt: str | None = None
    ) -> tuple[str, int]:
        """Resolve an embedding alias to (literal_model_id, embedding_dim) at KB creation.

        Prefers the llms-gateway ``GET /v1/models`` (finds the row whose ``id`` or
        ``alias`` matches and has a non-null ``embedding_dim``). Falls back to the
        env-pinned ``embedding_model_resolved`` / ``embedding_dim`` when llms is
        unreachable or in mock mode — so KB creation never hard-blocks on llms (the
        Component-1 / Component-10 circular-cold-start guard). The resolved pair is
        persisted IMMUTABLY on the KB.
        """
        fallback = (self._settings.embedding_model_resolved, self._settings.embedding_dim)
        if self._settings.mock_embeddings or self._tokens is None:
            return fallback
        try:
            service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
            headers = {
                "Authorization": f"Bearer {service_jwt}",
                "traceparent": trace.current_traceparent(),
                "X-Request-ID": trace.request_id_var.get(),
            }
            if agent_jwt:
                headers["X-Forwarded-Agent-JWT"] = agent_jwt
            url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/models"
            resp = await self._http().get(url, headers=headers)
            if resp.status_code >= 400:
                return fallback
            data = resp.json()
            models = data.get("data", data) if isinstance(data, dict) else data
            for m in models:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id") or m.get("model_id")
                aliases = m.get("aliases") or []
                dim = m.get("embedding_dim")
                if dim and (mid == alias or alias in aliases or m.get("alias") == alias):
                    return str(mid), int(dim)
            return fallback
        except Exception as exc:  # noqa: BLE001 — llms unreachable: env-pinned fallback
            logger.warning("embedding_model_resolve_fallback", alias=alias, error=str(exc))
            return fallback

    async def _embed_via_llms(
        self,
        texts: list[str],
        *,
        model: str,
        dim: int,
        agent_jwt: str | None,
        on_behalf_of: str | None,
        idempotency_key: str | None,
    ) -> EmbeddingResult:
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
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        body = {"model": model, "input": texts, "dimensions": dim}
        url = f"{self._settings.llms_gateway_url.rstrip('/')}/v1/embeddings"
        resp = await self._http().post(url, headers=headers, json=body)
        if resp.status_code == 429:
            # Surface rate limiting so the worker can back off per Retry-After.
            raise ApiError(
                ErrorCode.RATE_LIMIT_EXCEEDED,
                "Embedding provider rate limited.",
                headers={"Retry-After": resp.headers.get("Retry-After", "2")},
            )
        if resp.status_code >= 400:
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Embedding provider returned {resp.status_code}.",
            )
        data = resp.json()
        rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        vectors = [list(r["embedding"]) for r in rows]
        usage = data.get("usage") or {}
        metrics.embeddings_total.labels("llms").inc()
        return EmbeddingResult(
            vectors=vectors,
            model=data.get("model", model),
            prompt_tokens=int(usage.get("prompt_tokens", _estimate_tokens(texts))),
            source="llms",
        )


def _estimate_tokens(texts: list[str]) -> int:
    """~4 chars/token heuristic; minimum 1 (mirrors the llms mock estimate)."""
    return max(1, sum(len(t) for t in texts) // 4)
