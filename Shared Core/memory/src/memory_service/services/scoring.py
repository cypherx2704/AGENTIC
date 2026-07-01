"""Composite retrieval scoring (Stanford "Generative Agents") + importance heuristic.

This module is PURE (no DB, no network) so it is the single source of truth shared by the
Postgres repo and the in-memory repo, and is unit-tested directly.

Composite score (only used when ``MEMORY_SCORING_ENABLED`` is on)::

    composite = w_rec * recency + w_imp * importance + w_rel * relevance

Each component is normalized to ``[0, 1]`` BEFORE weighting so the weights are
comparable:

* **relevance** — cosine similarity remapped from ``[-1, 1]`` to ``[0, 1]`` (the same
  remap the wire ``similarity`` uses), so a perfect match is 1.0.
* **recency**   — exponential decay on the age since ``last_retrieved_at`` (falling back to
  ``last_accessed_at`` then ``created_at``): ``0.5 ** (age / half_life)``. Fresh = 1.0.
* **importance**— a stored, already-normalized ``importance_score`` in ``[0, 1]``.

When the flag is OFF the repos rank by pure cosine (relevance) exactly as before — this
module is simply not consulted, so today's behavior is byte-for-byte unchanged.

``heuristic_importance`` produces the default write-time importance: a deterministic,
network-free estimate from content length + a small set of salience keywords. An optional
LLM grader (behind ``MEMORY_IMPORTANCE_LLM_ENABLED``) may override it later; the heuristic
is always the safe default so keyless local dev works.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Keywords that bump a memory's salience. Deterministic + cheap; tuned for agent memory
# ("remember X", deadlines, preferences, identity facts) rather than chit-chat.
_SALIENCE_KEYWORDS: tuple[str, ...] = (
    "remember",
    "important",
    "always",
    "never",
    "must",
    "prefer",
    "favorite",
    "favourite",
    "deadline",
    "birthday",
    "password",
    "secret",
    "allerg",
    "name is",
    "my name",
    "i am",
    "i'm",
    "goal",
    "promise",
)


def clamp01(x: float) -> float:
    """Clamp ``x`` to the closed interval ``[0, 1]``."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def relevance_from_cosine(cosine: float) -> float:
    """Map a cosine similarity in ``[-1, 1]`` to a relevance in ``[0, 1]``."""
    return clamp01((cosine + 1.0) / 2.0)


def recency_score(
    *, reference: datetime | None, now: datetime, half_life_seconds: float
) -> float:
    """Exponential-decay recency in ``[0, 1]``: 1.0 when fresh, 0.5 at one half-life.

    ``reference`` is the timestamp the memory was last useful (last_retrieved_at, falling
    back to last_accessed_at / created_at). A ``None`` reference (or a future timestamp) is
    treated as maximally fresh (1.0). A non-positive half-life disables decay (always 1.0).
    """
    if reference is None or half_life_seconds <= 0.0:
        return 1.0
    age_seconds = (now - reference).total_seconds()
    if age_seconds <= 0.0:
        return 1.0
    return clamp01(0.5 ** (age_seconds / half_life_seconds))


def heuristic_importance(content: str, *, memory_type: str | None = None) -> float:
    """Deterministic, network-free importance estimate in ``[0, 1]`` for write time.

    Combines a saturating length signal (longer, more-specific memories tend to matter
    more, up to a cap) with a salience-keyword signal and a small per-type prior. The
    result is stable for a given input so tests + dedup math stay deterministic.
    """
    text = (content or "").strip().lower()
    if not text:
        return 0.0

    # Length signal: saturates around ~400 chars so a long essay isn't unboundedly "important".
    length_signal = clamp01(len(text) / 400.0)

    # Keyword salience: each distinct hit adds, saturating quickly.
    hits = sum(1 for kw in _SALIENCE_KEYWORDS if kw in text)
    keyword_signal = clamp01(hits / 3.0)

    # Per-type prior: facts/preferences are more durable than transient notes.
    type_prior = {
        "fact": 0.6,
        "preference": 0.7,
        "profile": 0.7,
        "decision": 0.6,
        "note": 0.4,
    }.get((memory_type or "note").lower(), 0.4)

    # Weighted blend, then clamp. Weights chosen so a plain short note lands ~0.3-0.4 and a
    # keyworded, substantive memory lands ~0.7-0.9.
    score = 0.35 * length_signal + 0.40 * keyword_signal + 0.25 * type_prior
    return round(clamp01(score), 6)


@dataclass(frozen=True)
class ScoringWeights:
    """Effective composite-score weights + recency half-life (from Settings)."""

    recency: float = 1.0
    importance: float = 1.0
    relevance: float = 1.0
    recency_half_life_seconds: float = 7 * 24 * 3600.0


def composite_score(
    *,
    cosine: float,
    importance: float,
    reference: datetime | None,
    now: datetime,
    weights: ScoringWeights,
) -> float:
    """The Generative-Agents composite in ``[0, 1]`` (weight-normalized).

    Each component is normalized to ``[0, 1]`` then weighted; the weighted sum is divided
    by the total weight so the result stays in ``[0, 1]`` regardless of the raw weights.
    """
    rel = relevance_from_cosine(cosine)
    rec = recency_score(
        reference=reference, now=now, half_life_seconds=weights.recency_half_life_seconds
    )
    imp = clamp01(importance)
    total_w = weights.recency + weights.importance + weights.relevance
    if total_w <= 0.0:
        return rel  # degenerate: fall back to pure relevance
    weighted = (
        weights.recency * rec + weights.importance * imp + weights.relevance * rel
    )
    return round(clamp01(weighted / total_w), 6)


def weights_from_settings(settings: object) -> ScoringWeights:
    """Build :class:`ScoringWeights` from a Settings-like object (duck-typed)."""
    return ScoringWeights(
        recency=float(getattr(settings, "memory_scoring_weight_recency", 1.0)),
        importance=float(getattr(settings, "memory_scoring_weight_importance", 1.0)),
        relevance=float(getattr(settings, "memory_scoring_weight_relevance", 1.0)),
        recency_half_life_seconds=float(
            getattr(settings, "memory_scoring_recency_half_life_seconds", 7 * 24 * 3600.0)
        ),
    )


def _now() -> datetime:
    return datetime.now(UTC)
