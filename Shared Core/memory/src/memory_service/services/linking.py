"""Associative memory linking (A-MEM / HippoRAG-style edge construction).

When ``MEMORY_LINKING_ENABLED`` is on, ingest generates explicit links from a new memory to
its nearest associative neighbours (same principal), and retrieval does a bounded 1-hop
expansion over those edges to surface memories the single-shot cosine missed.

This module is PURE (no DB). :func:`decide_links` is REAL deterministic logic: given the new
memory's nearest neighbours and their cosine similarities, it keeps the ones that are
ASSOCIATED (``sim_min <= cosine < dedup_threshold``) — related enough to link, but below the
dedup ceiling (at/above that the store path deduped, so there's no distinct neighbour to link
to). :func:`link_decision_llm` is the llms-gateway chat seam (mirrors ``_grade_importance_llm``):
no dedicated endpoint this cycle, so it returns ``None`` and the caller keeps the heuristic.

**Link construction + associative retrieval only** — NOT the gated consolidation summary and
NOT the gated supersession.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LinkCandidate:
    """A prospective edge endpoint: a neighbour memory id + its cosine to the new memory."""

    memory_id: str
    similarity: float


@dataclass(frozen=True)
class LinkDecision:
    """A decided edge: which neighbour to link, with what attribute + weight."""

    dst_memory_id: str
    relation: str
    weight: float


def decide_links(
    neighbours: list[LinkCandidate],
    *,
    sim_min: float,
    dedup_threshold: float,
    max_neighbors: int,
) -> list[LinkDecision]:
    """Pick which neighbours the new memory should link to (deterministic; no network).

    Keeps neighbours whose cosine is in ``[sim_min, dedup_threshold)`` — associatively related
    but not a near-duplicate — highest-similarity first, capped at ``max_neighbors``. The
    edge weight is the cosine (association strength). Returns an empty list when nothing
    qualifies (no spurious edges).
    """
    kept = [
        c for c in neighbours if sim_min <= c.similarity < dedup_threshold
    ]
    kept.sort(key=lambda c: c.similarity, reverse=True)
    return [
        LinkDecision(dst_memory_id=c.memory_id, relation="associated", weight=float(c.similarity))
        for c in kept[: max(max_neighbors, 0)]
    ]


async def link_decision_llm(
    gateway: object, new_content: str, neighbour_contents: list[str]
) -> list[LinkDecision] | None:
    """Optional llms-gateway link/attribute decision (behind a future flag).

    Skeleton mirroring ``_grade_importance_llm``: no dedicated linking endpoint on the
    gateway this cycle, so this returns ``None`` and the caller keeps :func:`decide_links`.
    A real implementation would ask the model which neighbours are genuinely related and with
    what relation, then fall back to the heuristic on any error.
    """
    return None
