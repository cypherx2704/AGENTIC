"""Output groundedness / hallucination signal (flagged; default off).

The heuristic backend scores lexical overlap with the provided context; below the configured
min score the output is flagged high-risk (review). The signal NEVER blocks on its own.
"""

from __future__ import annotations

from guardrails_service.core.config import Settings
from guardrails_service.services import groundedness as g


def test_grounded_response_scores_high() -> None:
    s = g.assess(
        response_text="The capital of France is Paris.",
        context_text="France is a country in Europe. Its capital is Paris.",
        settings=Settings(groundedness_min_score=0.4),
    )
    assert s.score >= 0.5
    assert s.high_risk is False


def test_ungrounded_response_flagged_high_risk() -> None:
    s = g.assess(
        response_text="The moon landing was filmed in a Burbank studio by aliens.",
        context_text="The user asked about the capital of France.",
        settings=Settings(groundedness_min_score=0.4),
    )
    assert s.score < 0.4
    assert s.high_risk is True


def test_no_context_is_failopen() -> None:
    # No context to check against -> we do not claim ungrounded (score 1.0, not flagged).
    s = g.assess(
        response_text="some answer", context_text="", settings=Settings()
    )
    assert s.score == 1.0
    assert s.high_risk is False


def test_empty_response_is_failopen() -> None:
    s = g.assess(response_text="", context_text="anything", settings=Settings())
    assert s.score == 1.0
    assert s.high_risk is False


def test_heuristic_score_bounds() -> None:
    assert g.heuristic_score("alpha beta gamma", "alpha beta gamma delta") == 1.0
    score = g.heuristic_score("alpha zeta", "alpha beta")
    assert 0.0 <= score <= 1.0
