"""Embedding generation via the llms-gateway, with a deterministic MOCK fallback.

The Memory service does NOT run an embedding model locally. It calls the llms-gateway
embeddings surface (``POST /v1/embeddings`` — the WP06 blocking deliverable) for real
vectors. To keep the service (and its tests) runnable with NO network, a deterministic
in-process pseudo-embedder is the fallback:

* ``settings.use_mock_embeddings`` True  -> ALWAYS use the mock (offline/dev/tests).
* otherwise                              -> try the gateway; on ANY failure (unreachable,
  timeout, non-200, malformed body) FALL OPEN to the mock and bump
  ``memory_embed_failopen_total``.

The mock derives each float from a SHA-256 digest of the text and L2-normalizes the
vector, so the SAME input always yields the SAME unit vector (tests assert determinism
and that semantically identical inputs are near-identical for dedup). Vector dimension
is fixed at ``settings.embeddings_vector_dim`` (1536).
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from typing import TYPE_CHECKING

import httpx
import structlog

from ..core import metrics
from ..core.config import Settings

if TYPE_CHECKING:
    from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


def pseudo_vector(text: str, dim: int) -> list[float]:
    """Deterministic L2-normalized pseudo-embedding of ``dim`` floats for ``text``.

    Seeded from a SHA-256 digest of the text so the same input always yields the same
    vector (tests can assert determinism + dedup) with no network. Values land in
    [-1, 1] and the vector is unit-normalized so it looks like a real embedding.
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


class EmbeddingClient:
    """Resolves text -> vector via the gateway, with a deterministic offline fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        tokens: ServiceTokenProvider | None = None,
        valkey: object | None = None,
    ) -> None:
        self._settings = settings
        # An injected AsyncClient (tests pass a respx-mocked one); otherwise lazily made.
        self._client = client
        self._owns_client = client is None
        # Optional Contract-12 service-token provider. When present (+ a forwarded agent JWT),
        # the gateway call carries the CALLER's tenant identity so it resolves that tenant's
        # BYOK key. Absent => falls back to the static embeddings_service_token (today's path).
        self._tokens = tokens
        # ── B2: content-hash embedding cache (Valkey; soft, fail-open) ─────────────────
        # Injected ValkeyClient-shaped object. Absent (or the flag off) => no caching, the
        # embed path is byte-identical to today.
        self._valkey = valkey

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.embeddings_base_url,
                timeout=self._settings.embeddings_timeout_seconds,
            )
        return self._client

    async def embed_one(
        self, text: str, *, on_behalf_of: str | None = None, agent_jwt: str | None = None
    ) -> tuple[list[float], str]:
        """Return ``(vector, source)`` for a single text. ``source`` is 'gateway'|'mock'."""
        vectors, source = await self.embed_many([text], on_behalf_of=on_behalf_of, agent_jwt=agent_jwt)
        return vectors[0], source

    async def embed_many(
        self, texts: list[str], *, on_behalf_of: str | None = None, agent_jwt: str | None = None
    ) -> tuple[list[list[float]], str]:
        """Embed a batch, transparently served from the content-hash cache when enabled.

        Returns ``(vectors, source)`` in INPUT ORDER. ``source`` is 'gateway'|'mock' when any
        text had to be embedded, else 'cache' (a full hit). The cache is EXACT: identical text
        under the same model+dim yields an identical vector, so a hit is never a semantic
        approximation. Cache misses embed once and write back. Any Valkey error FAILS OPEN
        (the batch is embedded normally). With the flag off / no Valkey this delegates
        straight to :meth:`_embed_uncached`, byte-identical to today.
        """
        if not (self._settings.memory_embedding_cache_enabled and self._valkey is not None):
            return await self._embed_uncached(texts, on_behalf_of=on_behalf_of, agent_jwt=agent_jwt)

        keys = [self._cache_key(t) for t in texts]
        cached = await self._cache_get_many(keys)  # list[vector | None]; fail-open => all None
        miss_idx = [i for i, v in enumerate(cached) if v is None]
        metrics.embed_cache_hits_total.inc(len(texts) - len(miss_idx))
        metrics.embed_cache_misses_total.inc(len(miss_idx))

        if not miss_idx:
            return [v for v in cached if v is not None], "cache"

        miss_texts = [texts[i] for i in miss_idx]
        fresh, source = await self._embed_uncached(
            miss_texts, on_behalf_of=on_behalf_of, agent_jwt=agent_jwt
        )
        await self._cache_set_many([keys[i] for i in miss_idx], fresh)  # fail-open
        for j, i in enumerate(miss_idx):
            cached[i] = fresh[j]
        return [v for v in cached if v is not None], source

    async def _embed_uncached(
        self, texts: list[str], *, on_behalf_of: str | None = None, agent_jwt: str | None = None
    ) -> tuple[list[list[float]], str]:
        """Resolve text -> vector with NO cache. Returns ``(vectors, source)`` ('gateway'|'mock').

        Mock mode (forced or fail-open) NEVER raises and NEVER hits the network. When a
        service-token provider + forwarded agent JWT are present, the gateway resolves the
        caller's tenant BYOK key (Contract-12 INTERNAL mode).
        """
        dim = self._settings.embeddings_vector_dim

        if self._settings.use_mock_embeddings:
            metrics.embed_calls_total.labels("mock").inc()
            return [pseudo_vector(t, dim) for t in texts], "mock"

        try:
            vectors = await self._call_gateway(texts, dim, on_behalf_of=on_behalf_of, agent_jwt=agent_jwt)
            metrics.embed_calls_total.labels("gateway").inc()
            return vectors, "gateway"
        except Exception as exc:  # noqa: BLE001 — gateway down: FALL OPEN to the mock
            logger.warning("embeddings_gateway_failed_fallback_mock", error=str(exc))
            metrics.embed_failopen_total.inc()
            metrics.embed_calls_total.labels("mock").inc()
            return [pseudo_vector(t, dim) for t in texts], "mock"

    # ── B2: content-hash cache helpers (exact; model+dim-namespaced; fail-open) ─────────
    def _cache_key(self, text: str) -> str:
        """Key = prefix + sha256(model \\x00 dim \\x00 NFC(text)).

        The model+dim namespace is INSIDE the hash, so a model or dimension change yields a
        completely different key space — a stale vector can never be served across a model/dim
        switch. Text is NFC-normalized only (canonical-equivalent unicode is the same string);
        no lossy transform, so the cached vector always corresponds to the exact embedded text.
        """
        norm = unicodedata.normalize("NFC", text)
        model = self._settings.embeddings_model
        dim = self._settings.embeddings_vector_dim
        digest = hashlib.sha256(f"{model}\x00{dim}\x00{norm}".encode()).hexdigest()
        return f"{self._settings.memory_embedding_cache_key_prefix}{digest}"

    async def _cache_get_many(self, keys: list[str]) -> list[list[float] | None]:
        """Fetch cached vectors for ``keys`` (input order). Any Valkey error => all misses."""
        out: list[list[float] | None] = []
        for key in keys:
            try:
                raw = await self._valkey.get(  # type: ignore[union-attr]
                    key, timeout_seconds=self._settings.valkey_ping_timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001 — cache is soft: FAIL OPEN to a miss
                logger.warning("embed_cache_get_failopen", error=str(exc))
                out.append(None)
                continue
            if not raw:
                out.append(None)
                continue
            try:
                vec = json.loads(raw)
            except (ValueError, TypeError):
                out.append(None)
                continue
            if isinstance(vec, list) and len(vec) == self._settings.embeddings_vector_dim:
                out.append([float(x) for x in vec])
            else:
                out.append(None)  # dim mismatch (stale) => treat as a miss, re-embed
        return out

    async def _cache_set_many(self, keys: list[str], vectors: list[list[float]]) -> None:
        """Write ``vectors`` back under ``keys`` with the configured TTL. Errors FAIL OPEN."""
        ttl = self._settings.memory_embedding_cache_ttl_seconds
        for key, vec in zip(keys, vectors, strict=True):
            try:
                await self._valkey.set(  # type: ignore[union-attr]
                    key, json.dumps(vec), ttl_seconds=ttl,
                    timeout_seconds=self._settings.valkey_ping_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort write-back
                logger.warning("embed_cache_set_failopen", error=str(exc))

    async def _call_gateway(
        self,
        texts: list[str],
        dim: int,
        *,
        on_behalf_of: str | None = None,
        agent_jwt: str | None = None,
    ) -> list[list[float]]:
        """POST /v1/embeddings to the llms-gateway and return the vectors (length == texts)."""
        client = self._ensure_client()
        headers = {"Content-Type": "application/json"}
        # Preferred: mint a Contract-12 service token (on_behalf_of=caller) + forward the agent
        # JWT, so the gateway resolves the CALLER tenant's BYOK key. Falls back to the static
        # token (legacy) when no provider/JWT is available.
        if self._tokens is not None and getattr(self._tokens, "enabled", False) and agent_jwt:
            service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
            headers["Authorization"] = f"Bearer {service_jwt}"
            headers["X-Forwarded-Agent-JWT"] = agent_jwt
        elif self._settings.embeddings_service_token:
            headers["Authorization"] = f"Bearer {self._settings.embeddings_service_token}"
        resp = await client.post(
            "/v1/embeddings",
            json={"model": self._settings.embeddings_model, "input": texts, "dimensions": dim},
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()
        data = sorted(body["data"], key=lambda d: d["index"])
        vectors = [d["embedding"] for d in data]
        if len(vectors) != len(texts):
            raise ValueError(
                f"gateway returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return vectors

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
