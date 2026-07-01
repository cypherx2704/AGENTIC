"""Output groundedness / hallucination signal (flagged; default OFF).

On ``/v1/check/output`` an OPTIONAL signal scores how well the model's response is supported
by the provided context (the original ``input_text`` plus any caller-supplied grounding
passages). A LOW groundedness score => HIGH hallucination risk, which is surfaced as a
'warn'-level REVIEW signal in decision metadata. The signal NEVER blocks on its own — it is
a review escalation, not enforcement — so enabling it cannot turn an allow into a block.

Default OFF (``groundedness_enabled=False``) => output checks are byte-identical to today.

Two backends:
  * ``heuristic`` (default, keyless) — a lexical-overlap entailment PROXY: the fraction of
    the response's content tokens that also appear in the context. Cheap, deterministic,
    no model download. A response with no context to check against scores 1.0 (we cannot
    claim it is ungrounded — fail-open, no false review).
  * ``llms_gateway`` — defers to the remote classify-variant (reusing the remote classifier
    transport + its short timeout/fallback). On any remote trouble it falls back to the
    heuristic so the signal is always answerable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from ..core.config import Settings

logger = structlog.get_logger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# Common stopwords ignored when measuring content overlap (so filler does not inflate the
# grounded fraction). Small + fixed; no NLP dependency.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
        "to", "of", "in", "on", "at", "for", "with", "as", "by", "it", "this", "that",
        "i", "you", "he", "she", "they", "we", "your", "my", "our", "their", "its",
        "do", "does", "did", "can", "will", "would", "should", "could", "have", "has",
        "had", "not", "no", "yes", "so", "if", "then", "than", "from", "up", "out",
    }
)


@dataclass(frozen=True)
class GroundednessSignal:
    """The groundedness assessment (additive decision metadata)."""

    score: float          # [0,1]; 1.0 = fully supported by context
    high_risk: bool       # score < configured min => flag for review
    backend: str


def _content_tokens(text: str) -> list[str]:
    return [t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text)) if t not in _STOPWORDS]


def heuristic_score(response_text: str, context_text: str) -> float:
    """Lexical-overlap entailment proxy in [0,1].

    Fraction of the response's content tokens that also appear in the context. With no
    response content tokens, or no context to compare against, returns 1.0 (fail-open: we
    do not flag what we cannot evaluate).
    """
    resp_tokens = _content_tokens(response_text)
    if not resp_tokens:
        return 1.0
    ctx_tokens = set(_content_tokens(context_text))
    if not ctx_tokens:
        return 1.0
    supported = sum(1 for t in resp_tokens if t in ctx_tokens)
    return round(supported / len(resp_tokens), 3)


def assess(
    *,
    response_text: str,
    context_text: str,
    settings: Settings,
) -> GroundednessSignal:
    """Compute the groundedness signal for an output check (heuristic backend).

    The remote (``llms_gateway``) backend is invoked by the check handler when configured
    (it is async); this synchronous helper is the keyless heuristic used by default and as
    the remote fallback.
    """
    score = heuristic_score(response_text, context_text)
    high_risk = score < settings.groundedness_min_score
    return GroundednessSignal(score=score, high_risk=high_risk, backend="heuristic")
