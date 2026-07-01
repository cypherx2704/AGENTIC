"""Offline eval harness: cosine vs composite retrieval scoring (recall@k + helpfulness).

Loads ``golden_memories.json``, stores it in the in-memory repository with the golden
``importance`` + ``age_days``, then runs each query under BOTH rankings (pure cosine and
the Generative-Agents composite) and reports recall@k + a helpfulness proxy. Exits non-zero
if composite REGRESSES recall@k vs cosine on the golden set, so it doubles as a CI guard.

Uses a deterministic bag-of-words embedder (no network, no heavy model) so semantically
related text has a high cosine — that makes recall@k meaningful and keeps the harness
runnable in the offline test environment exactly like the rest of the suite.

Run:
    ./.venv/Scripts/python.exe eval/run_eval.py
    ./.venv/Scripts/python.exe eval/run_eval.py --json
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make ``src`` importable when run directly from the repo root.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from memory_service.services.repository import (  # noqa: E402
    InMemoryRepository,
    StoredMemory,
    new_memory,
)
from memory_service.services.scoring import ScoringWeights  # noqa: E402

_TENANT = "00000000-0000-0000-0000-0000000000aa"
_PTYPE = "agent"
_PID = "eval-agent"
# top_k=1 makes ORDER decisive: when a stale distractor ties the target on cosine, only the
# correctly-ranked memory lands in the top-1, so recall@1 directly measures the re-rank win.
_TOP_K = 1
# A 60-day recency half-life keeps month-old high-importance facts competitive while still
# decaying year-old stale memories — the regime where composite helps without over-forgetting.
_RECENCY_HALF_LIFE_S = 60 * 24 * 3600.0
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _vocab(memories: list[dict], queries: list[dict]) -> list[str]:
    """Stable sorted vocabulary across all golden text (deterministic dimension order)."""
    words: set[str] = set()
    for m in memories:
        words.update(_TOKEN_RE.findall(m["content"].lower()))
    for q in queries:
        words.update(_TOKEN_RE.findall(q["query"].lower()))
    return sorted(words)


def _bow_vector(text: str, vocab: list[str]) -> list[float]:
    """L2-normalized bag-of-words vector over ``vocab`` (related text -> high cosine)."""
    counts: dict[str, int] = dict.fromkeys(vocab, 0)
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok in counts:
            counts[tok] += 1
    vec = [float(counts[w]) for w in vocab]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@dataclass
class Metrics:
    recall_at_k: float
    helpfulness: float  # mean normalized rank of the expected memory (1.0 best)
    hits: int
    total: int


async def _load_repo(memories: list[dict], vocab: list[str]) -> InMemoryRepository:
    repo = InMemoryRepository()
    now = datetime.now(UTC)
    for spec in memories:
        mem: StoredMemory = new_memory(
            tenant_id=_TENANT, principal_type=_PTYPE, principal_id=_PID, scope="principal_only",
            type=spec.get("type", "note"), tags=[], content=spec["content"], metadata={},
            vector=_bow_vector(spec["content"], vocab), session_id=None, ttl_seconds=None,
            importance_score=float(spec["importance"]),
        )
        # Force the golden id + age so recency/importance can diverge from cosine.
        mem.id = spec["id"]
        created = now - timedelta(days=float(spec.get("age_days", 0)))
        mem.created_at = created
        mem.last_accessed_at = created
        mem.last_retrieved_at = created
        repo._memories[mem.id] = mem  # noqa: SLF001 — eval harness, direct seed is fine
    return repo


async def _eval(
    repo: InMemoryRepository, queries: list[dict], vocab: list[str], *, scoring_enabled: bool
) -> Metrics:
    hits = 0
    rank_scores: list[float] = []
    weights = ScoringWeights(recency_half_life_seconds=_RECENCY_HALF_LIFE_S)
    for q in queries:
        qv = _bow_vector(q["query"], vocab)
        expected = q["expected"]
        # recall@k uses the configured top_k window.
        top = await repo.search(
            tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID, query_vector=qv,
            top_k=_TOP_K, type_filter=None, tags_filter=None, include_shared=True,
            user_scope_visibility="isolated", scoring_enabled=scoring_enabled,
            scoring_weights=weights, current_only=False,
        )
        if expected in [m.id for m in top]:
            hits += 1
        # helpfulness uses the FULL ranking so it captures rank improvements below top_k.
        full = await repo.search(
            tenant_id=_TENANT, caller_type=_PTYPE, caller_id=_PID, query_vector=qv,
            top_k=1000, type_filter=None, tags_filter=None, include_shared=True,
            user_scope_visibility="isolated", scoring_enabled=scoring_enabled,
            scoring_weights=weights, current_only=False,
        )
        full_ids = [m.id for m in full]
        rank_scores.append(1.0 / (full_ids.index(expected) + 1) if expected in full_ids else 0.0)
    total = len(queries)
    return Metrics(
        recall_at_k=hits / total if total else 0.0,
        helpfulness=sum(rank_scores) / total if total else 0.0,
        hits=hits,
        total=total,
    )


def _read_golden(golden_path: Path) -> dict:
    return json.loads(golden_path.read_text(encoding="utf-8"))


async def run(golden_path: Path) -> tuple[Metrics, Metrics]:
    data = _read_golden(golden_path)
    memories, queries = data["memories"], data["queries"]
    vocab = _vocab(memories, queries)
    cosine = await _eval(await _load_repo(memories, vocab), queries, vocab, scoring_enabled=False)
    composite = await _eval(
        await _load_repo(memories, vocab), queries, vocab, scoring_enabled=True
    )
    return cosine, composite


def _print_table(cosine: Metrics, composite: Metrics) -> None:
    print(f"Golden queries: {cosine.total}   top_k={_TOP_K}\n")
    print(f"{'metric':<20}{'cosine':>12}{'composite':>12}{'delta':>10}")
    print("-" * 54)
    for name, c, k in (
        ("recall@k", cosine.recall_at_k, composite.recall_at_k),
        ("helpfulness", cosine.helpfulness, composite.helpfulness),
    ):
        print(f"{name:<20}{c:>12.3f}{k:>12.3f}{(k - c):>+10.3f}")
    print()
    verdict = "PASS" if composite.recall_at_k >= cosine.recall_at_k else "FAIL"
    print(f"composite recall@k >= cosine recall@k -> {verdict}")


def main() -> int:
    golden = Path(__file__).resolve().parent / "golden_memories.json"
    cosine, composite = asyncio.run(run(golden))
    if "--json" in sys.argv:
        print(json.dumps({
            "top_k": _TOP_K,
            "cosine": cosine.__dict__,
            "composite": composite.__dict__,
            "composite_no_regression": composite.recall_at_k >= cosine.recall_at_k,
        }, indent=2))
    else:
        _print_table(cosine, composite)
    return 0 if composite.recall_at_k >= cosine.recall_at_k else 1


if __name__ == "__main__":
    raise SystemExit(main())
