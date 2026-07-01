"""Rerank providers — pluggable, deterministic MOCK default + local cross-encoder seam.

Mirrors how chat/embeddings select a provider (mock vs real):

* :class:`MockRerankProvider` (DEFAULT, ``RERANK_PROVIDER=mock``) — a deterministic
  lexical-overlap scorer. No keys, no network, no heavy model deps. Same query +
  documents always yield the same scores/order, so the surface is stable offline and
  in CI. This is the keyless local-dev / unit-test reranker.

* :class:`LocalRerankProvider` (``RERANK_PROVIDER=local``) — the seam for a real
  cross-encoder (bge-reranker class). It is NOT wired into the default image (no heavy
  model deps are added there); until a model runtime is provisioned it raises a clear
  Contract-2 ``SERVICE_UNAVAILABLE``. Provisioning is a later, additive change behind
  the same flag — the default behaviour (mock) is untouched.

The scoring contract (both): higher score == more relevant; results are returned sorted
by descending score, each pointing back at its request-document ``index`` (and echoing
the caller ``id`` when present). Rerank has no completion tokens — usage carries an
optional processed-token estimate + ``search_units`` (Contract-19 metering UNITS).
"""

from __future__ import annotations

import re

from ...core.config import Settings
from ...core.errors import ApiError, ErrorCode
from ...models.unified import (
    RerankRequest,
    RerankResponse,
    RerankResult,
    RerankUsage,
)
from .base import NonChatProvider, ProviderAdaptor

# Dedicated in-house provider key for the rerank/safety surfaces (distinct from the
# anthropic/openai chat+embeddings providers). Matches the seed pricing/capability rows.
RERANK_PROVIDER = "cypherx"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _estimate_tokens(text: str) -> int:
    # ~4 chars/token heuristic; minimum 1 so a usage estimate is always present.
    return max(1, len(text) // 4)


def _lexical_overlap_score(query_terms: set[str], doc_text: str) -> float:
    """Deterministic relevance in [0, 1]: Jaccard-ish overlap of query terms in the doc.

    score = (# distinct query terms present in the doc) / (# distinct query terms),
    with a small length-normalized term-frequency bonus so a doc that repeats matching
    terms ranks above one that mentions them once. Purely a function of the input text,
    so the same (query, documents) always produces the same scores — stable offline.
    """
    if not query_terms:
        return 0.0
    doc_terms = _tokens(doc_text)
    if not doc_terms:
        return 0.0
    doc_set = set(doc_terms)
    matched = query_terms & doc_set
    coverage = len(matched) / len(query_terms)
    # Term-frequency bonus, length-normalized and bounded so coverage dominates.
    hits = sum(1 for t in doc_terms if t in query_terms)
    tf_bonus = min(0.0999, hits / (len(doc_terms) + 1) * 0.1)
    return round(min(1.0, coverage * 0.9 + tf_bonus), 6)


class MockRerankProvider(NonChatProvider):
    """Deterministic, keyless reranker (the default)."""

    provider = RERANK_PROVIDER

    async def rerank(self, req: RerankRequest, *, model_id: str) -> RerankResponse:
        query_terms = set(_tokens(req.query))
        scored: list[RerankResult] = [
            RerankResult(
                index=i,
                id=doc.id,
                score=_lexical_overlap_score(query_terms, doc.text),
            )
            for i, doc in enumerate(req.documents)
        ]
        # Sort by descending score; ties keep the original input order (stable sort on a
        # pre-indexed list) so the surface is fully deterministic.
        scored.sort(key=lambda r: r.score, reverse=True)
        if req.top_n is not None:
            scored = scored[: req.top_n]

        total_tokens = _estimate_tokens(req.query) + sum(
            _estimate_tokens(d.text) for d in req.documents
        )
        return RerankResponse(
            results=scored,
            model=model_id,
            usage=RerankUsage(
                total_tokens=total_tokens,
                # One billable search unit per candidate document scored (Contract-19).
                search_units=len(req.documents),
            ),
        )


class LocalRerankProvider(NonChatProvider):
    """Seam for a local cross-encoder (bge-reranker class). NOT in the default image.

    Selected only by ``RERANK_PROVIDER=local``. Loading a cross-encoder pulls heavy
    deps (torch / sentence-transformers) deliberately kept OUT of the default build, so
    until a model runtime is provisioned this raises a clear Contract-2 503 rather than
    silently degrading. Wiring an actual model is a later, additive change behind this
    same flag — the mock default is never affected.
    """

    provider = RERANK_PROVIDER

    def __init__(self, settings: Settings) -> None:
        self._model_id = settings.rerank_local_model

    async def rerank(self, req: RerankRequest, *, model_id: str) -> RerankResponse:
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "The local cross-encoder reranker is not provisioned in this image "
            "(set RERANK_PROVIDER=mock for the deterministic reranker).",
            status_code=503,
            details={
                "reason": "RERANK_LOCAL_UNAVAILABLE",
                "configured_model": self._model_id,
            },
        )


def get_rerank_provider(settings: Settings) -> ProviderAdaptor:
    """Select the rerank provider per ``RERANK_PROVIDER`` (default 'mock')."""
    if settings.rerank_provider == "local":
        return LocalRerankProvider(settings)
    return MockRerankProvider()
