"""Phase C — lightweight, dependency-free query-type classifier for intent-aware retrieval.

A regex/keyword heuristic (NO ML, no LLM, no latency cost) buckets a question into one of:
``ownership`` / ``dependency`` / ``expertise`` / ``timeline`` / ``reasoning`` / ``general``.
The orchestrator scales each retrieval leg's RRF contribution by ``LEG_WEIGHTS[type]`` so an
ownership/dependency question leans on the GRAPH leg while a "why/how" reasoning question
leans on the RAG (text) leg. This is the pragmatic mid-point between fixed RRF and an
expensive learned router (2024-2025 adaptive-retrieval surveys).
"""

from __future__ import annotations

import re

# (graph, keyword, rag) multipliers applied to each leg's RRF bump.
LEG_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "ownership": (1.6, 1.0, 0.6),
    "dependency": (1.6, 1.0, 0.6),
    "expertise": (1.5, 1.0, 0.7),
    "timeline": (1.5, 1.0, 0.6),
    "reasoning": (0.8, 0.9, 1.6),
    "general": (1.0, 1.0, 1.0),
}

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ownership", re.compile(r"\b(who owns|owner|owns|maintainer|maintains|responsible for|in charge of|codeowner)\b", re.I)),
    ("dependency", re.compile(r"\b(depend|depends on|dependenc|what breaks|impact|affected|blast radius|downstream|upstream|consumes|calls)\b", re.I)),
    ("expertise", re.compile(r"\b(expert|experts|who knows|who should|familiar with|specialist|best person|reviewer|review)\b", re.I)),
    ("timeline", re.compile(r"\b(what changed|recent|recently|history|over time|timeline|activity|when did|last (week|month|day)|since|latest commits?)\b", re.I)),
    ("reasoning", re.compile(r"\b(why|how does|how do|how is|explain|rationale|reason|decided|decision|purpose|what is)\b", re.I)),
]


def classify(question: str) -> str:
    """Return the query type. First strong match wins (order encodes priority: ownership and
    dependency are the highest-precision graph intents); falls back to 'general'."""
    if not question:
        return "general"
    for qtype, rx in _PATTERNS:
        if rx.search(question):
            return qtype
    return "general"


def leg_weights(qtype: str) -> tuple[float, float, float]:
    """(graph, keyword, rag) RRF multipliers for a query type."""
    return LEG_WEIGHTS.get(qtype, LEG_WEIGHTS["general"])
