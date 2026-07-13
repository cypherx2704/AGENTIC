"""Application-level Reciprocal Rank Fusion (RRF) over N ranked lists.

The hybrid retrieval path fuses its TWO legs (dense + lexical) with RRF *inside SQL*
(`store/pgvector.py:search_hybrid`), which is not reusable for fusing an arbitrary number of
independently-retrieved ranked lists. Multi-query expansion (RAG-Fusion) produces one ranked
list per query variant, so it needs a small in-process fusion over N lists — this module.

RRF is score-scale agnostic (it fuses *ranks*, never the incomparable per-list scores), which
is exactly why it is the right combiner for lists produced by different query embeddings.
"""

from __future__ import annotations

# Standard RRF constant (Cormack et al., 2009). Kept as the algorithm default; callers pass the
# service-configured value (``settings.hybrid_rrf_k``) so it stays tunable, never hardcoded.
DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]], *, k: int = DEFAULT_RRF_K
) -> list[tuple[str, float]]:
    """Fuse ``ranked_lists`` (each best-first) into one ranking via Reciprocal Rank Fusion.

    Each item's fused score is ``sum over lists of 1 / (k + rank)`` (rank is 1-based within a
    list). Returns ``(item, fused_score)`` pairs ordered by score DESC, ties broken by item id
    ASC — matching the SQL hybrid path's ``ORDER BY score DESC, chunk_id`` so the two fusions
    are consistent. An item absent from a list simply contributes nothing from that list.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
