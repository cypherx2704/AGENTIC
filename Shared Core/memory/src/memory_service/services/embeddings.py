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
import math

import httpx
import structlog

from ..core import metrics
from ..core.config import Settings

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
        tokens: "ServiceTokenProvider | None" = None,
    ) -> None:
        self._settings = settings
        # An injected AsyncClient (tests pass a respx-mocked one); otherwise lazily made.
        self._client = client
        self._owns_client = client is None
        # Optional Contract-12 service-token provider. When present (+ a forwarded agent JWT),
        # the gateway call carries the CALLER's tenant identity so it resolves that tenant's
        # BYOK key. Absent => falls back to the static embeddings_service_token (today's path).
        self._tokens = tokens

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
        """Embed a batch. Returns ``(vectors, source)`` where source is 'gateway'|'mock'.

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
