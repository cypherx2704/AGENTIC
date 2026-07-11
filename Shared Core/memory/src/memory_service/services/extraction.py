"""Salient-fact extraction at ingest (Mem0 / LangMem atomic-fact decomposition).

When ``MEMORY_EXTRACTION_ENABLED`` is on, a multi-fact write is decomposed into atomic,
self-contained facts, each stored as its own memory row with its own focused embedding —
instead of embedding a multi-fact blob as one averaged vector (which matches a query about
any single fact weakly, hurting recall@k).

This module is PURE (no DB). :func:`extract_facts` is REAL deterministic split logic that is
always available (no network, keyless dev/tests). :func:`extract_facts_llm` is the
llms-gateway chat seam, mirroring the ``_grade_importance_llm`` shape: there is no dedicated
extraction endpoint on the gateway this cycle, so it returns ``None`` (the caller keeps the
deterministic split). Wiring a real gateway prompt later is additive.

**Extraction only** — deliberately NOT the gated contradiction reconciliation and NOT the
gated consolidation summary.
"""

from __future__ import annotations

import re

# Sentence-boundary split: a terminator (. ! ?) followed by whitespace. Also split on hard
# separators that authors use to list distinct facts (newlines, semicolons, bullets).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\n\r;]+|\s+[-*•]\s+")
# Coordinating conjunctions that usually join two independent clauses into one "sentence".
# Splitting on these recovers atomic facts like "I like tea and I hate coffee".
_CONJUNCTION_SPLIT = re.compile(r"\s+\b(?:and also|and|but|however)\b\s+", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

_MIN_FACT_WORDS = 2  # a "fact" needs at least this many content words to stand alone


def _normalize_fact(fragment: str) -> str:
    """Trim a fragment and drop a leading list bullet / numbering."""
    frag = fragment.strip()
    frag = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", frag)
    return frag.strip()


def _has_clause(fragment: str) -> bool:
    """True when a fragment carries enough content words to be a standalone fact."""
    return len(_WORD_RE.findall(fragment)) >= _MIN_FACT_WORDS


def extract_facts(content: str, *, max_facts: int = 16) -> list[str]:
    """Decompose ``content`` into atomic, self-contained facts (deterministic; no network).

    Splits on sentence terminators and hard separators, then further splits long
    conjunction-joined clauses. Fragments are trimmed, de-bulleted, de-duplicated
    (case-insensitively, order-preserving) and filtered to those with real content. Returns
    at most ``max_facts`` facts. When the content is a single fact (or cannot be split into
    two standalone facts) the ORIGINAL content is returned as a single-element list, so the
    caller stores it exactly as today (no fan-out, byte-identical).
    """
    text = (content or "").strip()
    if not text:
        return [text]

    raw_parts = [p for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    facts: list[str] = []
    for part in raw_parts:
        clause = _normalize_fact(part)
        if not clause:
            continue
        # Only split a clause on conjunctions when BOTH sides would stand alone as facts.
        sub = [_normalize_fact(s) for s in _CONJUNCTION_SPLIT.split(clause)]
        sub = [s for s in sub if s]
        if len(sub) > 1 and all(_has_clause(s) for s in sub):
            facts.extend(sub)
        else:
            facts.append(clause)

    # De-duplicate case-insensitively, preserve first-seen order, keep only real facts.
    seen: set[str] = set()
    unique: list[str] = []
    for f in facts:
        if not _has_clause(f):
            continue
        key = f.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    if len(unique) <= 1:
        # Nothing to gain from fan-out — store the original content unchanged.
        return [text]
    return unique[:max_facts]


async def extract_facts_llm(
    embedder_or_gateway: object, content: str, *, max_facts: int = 16
) -> list[str] | None:
    """Optional llms-gateway chat extraction (behind MEMORY_EXTRACTION_LLM_ENABLED).

    Skeleton mirroring ``_grade_importance_llm``: there is no dedicated extraction endpoint
    on the llms-gateway in this cycle, so this returns ``None`` and the caller keeps the
    deterministic :func:`extract_facts` split. The flag + seam exist so a real gateway prompt
    is a purely additive follow-up (parse the model's atomic-fact list, fall back to the
    heuristic on any error).
    """
    return None
