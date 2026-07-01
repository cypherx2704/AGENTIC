"""Contradiction / temporal-validity detection (supersession).

When ``MEMORY_CONTRADICTION_ENABLED`` is on, a store may CONFLICT with a prior memory of
the same principal: the two are clearly about the same subject (high embedding similarity
AND a lexical-overlap signal) but are NOT a dedup-level near-identical copy (otherwise the
store path already deduped/bumped). In that case the prior memory is marked SUPERSEDED
(``valid_until`` + ``superseded_by_id`` set) rather than deleted, and search returns only
current memories by default. The historical row is preserved for audit.

This module is PURE (no DB) so the Postgres + in-memory repos share the exact same
decision. The signal is intentionally conservative + Postgres-native-friendly (it mirrors
what a ``tsvector`` lexical overlap + pgvector cosine would compute) and never fires for
exact duplicates, which the dedup path owns.
"""

from __future__ import annotations

import re

# Words that signal a value/state assertion that a newer memory can overturn. Helps avoid
# superseding two unrelated facts that merely share a topic noun.
_ASSERTION_HINTS: tuple[str, ...] = (
    "is",
    "are",
    "was",
    "were",
    "prefer",
    "prefers",
    "favorite",
    "favourite",
    "now",
    "instead",
    "no longer",
    "changed",
    "updated",
    "moved",
    "lives",
    "works",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Tiny English stop-list so lexical overlap reflects content words, not glue.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "of", "to", "and", "or", "in", "on", "at", "for", "with",
        "my", "your", "his", "her", "its", "our", "their", "i", "you", "he", "she",
        "it", "we", "they", "this", "that", "these", "those", "as", "by", "from",
    }
)


def _content_tokens(text: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 1
    }


def jaccard_overlap(a: str, b: str) -> float:
    """Jaccard overlap of the content-word sets of ``a`` and ``b`` in ``[0, 1]``.

    A lightweight, deterministic stand-in for a ``tsvector`` lexical match — high when two
    memories talk about the same things, low when they don't.
    """
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _has_assertion(text: str) -> bool:
    low = f" {(text or '').lower()} "
    return any(f" {h} " in low for h in _ASSERTION_HINTS)


def is_contradiction(
    *,
    new_content: str,
    prior_content: str,
    cosine_similarity: float,
    sim_min: float,
    dedup_threshold: float,
    min_lexical_overlap: float = 0.2,
) -> bool:
    """Return True iff the NEW memory should SUPERSEDE the PRIOR one.

    Conditions (all must hold):

    * embedding similarity is high enough to be "about the same thing"
      (``sim_min <= cosine < dedup_threshold``) — at/above ``dedup_threshold`` it's an
      exact-ish copy the dedup path already handles, so we never supersede there;
    * there is real lexical overlap (same subject words), but the contents are not
      identical (an identical copy is a dedup, not a contradiction);
    * at least one side makes a value/state assertion that a newer memory can overturn.
    """
    if cosine_similarity < sim_min or cosine_similarity >= dedup_threshold:
        return False
    if new_content.strip().lower() == prior_content.strip().lower():
        return False  # identical -> dedup territory, not a contradiction
    overlap = jaccard_overlap(new_content, prior_content)
    if overlap < min_lexical_overlap:
        return False
    return _has_assertion(new_content) or _has_assertion(prior_content)
