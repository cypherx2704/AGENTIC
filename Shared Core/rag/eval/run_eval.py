"""Retrieval eval harness — recall@k / nDCG@k / Context-Precision@k / MRR.

Runs ENTIRELY against the in-process fakes (``tests/fakes.py`` FakeDb/FakePool) + the
deterministic mock embedder + mock reranker, so it needs NO Postgres / llms-gateway / network
— ``uv run python -m eval.run_eval`` (or ``.venv/Scripts/python.exe eval/run_eval.py``).

It loads ``eval/golden_kb.json`` (a tiny golden KB: one chunk per doc + queries annotated
with their relevant chunk ids), indexes every doc with the mock embedding (mirroring the real
ingest path's content_tsv via the fake), then evaluates several configurations:

  * ``dense``         — the default two-pass pgvector path (PgVectorAdapter.search).
  * ``hybrid``        — dense + lexical fused with Reciprocal Rank Fusion (search_hybrid).
  * ``hybrid+rerank`` — hybrid candidates re-ordered by the mock cross-encoder reranker.
  * ``decompose``     — multi-hop: split → retrieve per sub-question → union/dedup → rerank
                        (measured on the ``multihop`` golden slice, its own corpus).
  * ``multi_query``   — RAG-Fusion: expand → retrieve per variant → app-level RRF fusion
                        (measured on the ``multiquery`` golden slice, its own corpus).

Metrics (averaged over the query set), reported at k = 1, 3, 5, 10:
  * recall@k — fraction of a query's relevant chunks present in the top-k.
  * nDCG@k   — rank-discounted gain (binary relevance), normalized to the ideal ordering.
  * CP@k     — RAGAS-style ID-based Context Precision (mean Precision@i over the ranks a
               relevant chunk appears): the window signal-to-noise the caller's LLM sees.
  * MRR      — reciprocal rank of the FIRST relevant chunk (rank-cutoff independent).

Exit codes (regression gates the test suite / CI can call):
  * ``--assert-hybrid-ge-dense``   — non-zero if hybrid's mean nDCG@5 is below dense's.
  * ``--assert-rerank-precision``  — non-zero if hybrid+rerank's mean CP@5 is below hybrid's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

# Make both ``src`` (the service) and the repo root (for ``tests.fakes``) importable when run
# as a script or as ``-m eval.run_eval``.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from tests.fakes import FakeDb, FakePool  # noqa: E402

from rag_service.core.config import Settings  # noqa: E402
from rag_service.services.decompose import mock_decompose  # noqa: E402
from rag_service.services.embeddings import mock_embed  # noqa: E402
from rag_service.services.fusion import reciprocal_rank_fusion  # noqa: E402
from rag_service.services.rerank import mock_rerank  # noqa: E402
from rag_service.services.store.pgvector import PgVectorAdapter  # noqa: E402

TENANT = "00000000-0000-0000-0000-0000000000aa"
KB = "kb-eval"
DIM = 1536
KS = (1, 3, 5, 10)
# Sub-question cap for the eval's deterministic decomposition (mirrors decompose_max_subquestions).
DECOMPOSE_MAX_SUBQ = 4

# Base configs measured on the shared ``queries`` slice; evaluate() returns exactly these by
# default so the CI harness contract (dense/hybrid/hybrid+rerank) is unchanged.
DEFAULT_CONFIGS = ("dense", "hybrid", "hybrid+rerank")


@dataclass
class MetricRow:
    config: str
    recall: dict[int, float]
    ndcg: dict[int, float]
    cp: dict[int, float]  # Context-Precision@k (ID-based MAP)
    mrr: float


def _load_golden(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed(db: FakeDb, documents: list[dict]) -> None:
    for d in documents:
        db.chunks.append({
            "chunk_id": d["id"], "doc_id": d["id"], "kb_id": KB, "tenant_id": TENANT,
            "content": d["content"], "chunk_index": 0, "embedding_model": "m",
            "embedding_dim": DIM, "metadata": {"doc_name": d["id"]}, "created_at": None,
        })
        db.chunk_vectors_1536.append({
            "chunk_id": d["id"], "tenant_id": TENANT, "kb_id": KB,
            "embedding": mock_embed([d["content"]], DIM)[0],
        })


def _adapter_for(documents: list[dict]) -> PgVectorAdapter:
    """Seed a fresh in-memory store with ``documents`` and return its adapter."""
    db = FakeDb()
    _seed(db, documents)
    return PgVectorAdapter(FakePool(db), Settings(mock_embeddings=True))


def _recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = ranked[:k]
    return len(set(top) & relevant) / len(relevant)


def _ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, cid in enumerate(ranked[:k]) if cid in relevant)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return (dcg / idcg) if idcg > 0 else 0.0


def _context_precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """RAGAS-style ID-based Context Precision@k — a pure-arithmetic MAP over the golden set.

    ``CP@k = Σ_{i=1..k} (Precision@i · rel_i) / (# relevant chunks present in the top k)`` where
    ``rel_i`` ∈ {0,1} marks a relevant hit at rank i and ``Precision@i`` = (# relevant in top i)
    / i. Unlike recall@k (blind to where non-relevant items land) and nDCG@k (ideal-normalized),
    this isolates *window precision*: how many distractors sit among the relevant chunks the
    caller's LLM will read. Needs no LLM and no reference answer — just the relevant-id list.
    Returns 0.0 when no relevant chunk appears in the top k (or the query has no relevant set).
    Collapses to MRR on single-relevant queries; genuinely distinct on multi-relevant ones.
    """
    if not relevant:
        return 0.0
    hits = 0
    weighted = 0.0
    for i, cid in enumerate(ranked[:k], start=1):
        if cid in relevant:
            hits += 1
            weighted += hits / i  # Precision@i at this relevant rank
    return (weighted / hits) if hits else 0.0


def _mrr(ranked: list[str], relevant: set[str]) -> float:
    for i, cid in enumerate(ranked):
        if cid in relevant:
            return 1.0 / (i + 1)
    return 0.0


async def _rank_dense(adapter: PgVectorAdapter, q: dict, *, top_k: int) -> list[str]:
    hits = await adapter.search(
        TENANT, KB, mock_embed([q["query"]], DIM)[0],
        top_k=top_k, min_score=-1.0, filters=None, dimension=DIM, ef_search=100,
    )
    return [h.chunk_id for h in hits]


async def _rank_hybrid(adapter: PgVectorAdapter, q: dict, *, top_k: int) -> list[str]:
    hits = await adapter.search_hybrid(
        TENANT, KB, mock_embed([q["query"]], DIM)[0], q["query"],
        top_k=top_k, candidates=50, rrf_k=60, filters=None, dimension=DIM, ef_search=100,
        mode="hybrid",
    )
    return [h.chunk_id for h in hits]


async def _rank_hybrid_rerank(adapter: PgVectorAdapter, q: dict, *, top_k: int) -> list[str]:
    # Retrieve a wider hybrid candidate pool, then re-order with the mock reranker.
    hits = await adapter.search_hybrid(
        TENANT, KB, mock_embed([q["query"]], DIM)[0], q["query"],
        top_k=max(top_k, 20), candidates=50, rrf_k=60, filters=None, dimension=DIM, ef_search=100,
        mode="hybrid",
    )
    docs = [h.content for h in hits]
    items = mock_rerank(q["query"], docs, top_n=top_k)
    return [hits[it.index].chunk_id for it in items]


async def _rank_decompose(adapter: PgVectorAdapter, q: dict, *, top_k: int) -> list[str]:
    """B2 harness ranker: deterministic decomposition → retrieve per sub-question (hybrid) →
    union/dedup by chunk_id → mock cross-encoder rerank over the merged pool.

    Mirrors the service ``api/query.py`` decompose path: ``mock_decompose`` splits the query,
    each sub-question is retrieved via the same hybrid leg (mock_embed dense + lexical), the
    pools are unioned keeping the best per-chunk score, then the merged pool is reranked against
    the ORIGINAL query. The union is what recovers multi-hop facts a single query ranks apart.
    """
    sub_questions = mock_decompose(q["query"], DECOMPOSE_MAX_SUBQ)
    merged: dict[str, object] = {}
    for sub in sub_questions:
        hits = await adapter.search_hybrid(
            TENANT, KB, mock_embed([sub], DIM)[0], sub,
            top_k=max(top_k, 20), candidates=50, rrf_k=60, filters=None, dimension=DIM,
            ef_search=100, mode="hybrid",
        )
        for h in hits:
            prev = merged.get(h.chunk_id)
            if prev is None or h.score > prev.score:  # type: ignore[attr-defined]
                merged[h.chunk_id] = h
    pool = sorted(merged.values(), key=lambda h: (h.score, h.chunk_id), reverse=True)  # type: ignore[attr-defined]
    docs = [h.content for h in pool]  # type: ignore[attr-defined]
    items = mock_rerank(q["query"], docs, top_n=top_k)
    return [pool[it.index].chunk_id for it in items]  # type: ignore[attr-defined]


async def _rank_multiquery(adapter: PgVectorAdapter, q: dict, *, top_k: int) -> list[str]:
    """B3 harness ranker: hand-authored query variants → retrieve per variant (hybrid) → fuse
    with the app-level Reciprocal Rank Fusion (k=60).

    The mock embedder is non-semantic, so recall is demonstrated via authored variants whose
    vocabulary matches the relevant chunk (a vocabulary-mismatch the original wording misses).
    Mirrors the service multi-query path: original query + variants, per-variant retrieval,
    ``reciprocal_rank_fusion`` over the N ranked lists.
    """
    variants = [q["query"], *q.get("variants", [])]
    ranked_lists: list[list[str]] = []
    for variant in variants:
        hits = await adapter.search_hybrid(
            TENANT, KB, mock_embed([variant], DIM)[0], variant,
            top_k=max(top_k, 20), candidates=50, rrf_k=60, filters=None, dimension=DIM,
            ef_search=100, mode="hybrid",
        )
        ranked_lists.append([h.chunk_id for h in hits])
    fused = reciprocal_rank_fusion(ranked_lists, k=60)
    return [cid for cid, _score in fused[:top_k]]


_RANKERS = {
    "dense": _rank_dense,
    "hybrid": _rank_hybrid,
    "hybrid+rerank": _rank_hybrid_rerank,
    "decompose": _rank_decompose,
    "multi_query": _rank_multiquery,
}

# Which golden slice (corpus + queries) each config is measured on. Base configs share the
# top-level ``documents``/``queries``; the query-transformation configs use their own crafted
# corpus so they never perturb the base metrics (and vice-versa).
_SLICE_FOR = {
    "dense": (None, "queries"),
    "hybrid": (None, "queries"),
    "hybrid+rerank": (None, "queries"),
    "decompose": ("multihop", "queries"),
    "multi_query": ("multiquery", "queries"),
}


def _slice_data(golden: dict, config: str) -> tuple[list[dict], list[dict]]:
    """Return ``(documents, queries)`` for a config's golden slice."""
    section, queries_key = _SLICE_FOR[config]
    if section is None:
        return golden["documents"], golden["queries"]
    sub = golden[section]
    return sub["documents"], sub[queries_key]


async def _run_config(adapter: PgVectorAdapter, config: str, queries: list[dict]) -> MetricRow:
    ranker = _RANKERS[config]
    max_k = max(KS)
    recall = dict.fromkeys(KS, 0.0)
    ndcg = dict.fromkeys(KS, 0.0)
    cp = dict.fromkeys(KS, 0.0)
    mrr = 0.0
    for q in queries:
        relevant = set(q["relevant"])
        ranked = await ranker(adapter, q, top_k=max_k)
        for k in KS:
            recall[k] += _recall_at_k(ranked, relevant, k)
            ndcg[k] += _ndcg_at_k(ranked, relevant, k)
            cp[k] += _context_precision_at_k(ranked, relevant, k)
        mrr += _mrr(ranked, relevant)
    n = len(queries) or 1
    return MetricRow(
        config=config,
        recall={k: recall[k] / n for k in KS},
        ndcg={k: ndcg[k] / n for k in KS},
        cp={k: cp[k] / n for k in KS},
        mrr=mrr / n,
    )


async def evaluate(golden: dict, configs: tuple[str, ...] = DEFAULT_CONFIGS) -> list[MetricRow]:
    """Evaluate each requested config on its golden slice. Adapters are built once per corpus.

    Default ``configs`` = the three base retrieval configs on the shared ``queries`` slice, so
    ``evaluate(golden)`` is the unchanged CI contract. Pass ``("decompose",)`` / ``("multi_query",)``
    (with the corresponding golden sections present) to score the query-transformation configs.
    """
    adapters: dict[int, PgVectorAdapter] = {}
    rows: list[MetricRow] = []
    for config in configs:
        documents, queries = _slice_data(golden, config)
        key = id(documents)  # same list object ⇒ reuse the seeded adapter
        adapter = adapters.get(key)
        if adapter is None:
            adapter = _adapter_for(documents)
            adapters[key] = adapter
        rows.append(await _run_config(adapter, config, queries))
    return rows


def _configs_for(golden: dict) -> tuple[str, ...]:
    """Base configs, plus the query-transformation configs whose golden slices are present."""
    configs = list(DEFAULT_CONFIGS)
    if "multihop" in golden:
        configs.append("decompose")
    if "multiquery" in golden:
        configs.append("multi_query")
    return tuple(configs)


def _print_table(rows: list[MetricRow]) -> None:
    header = (
        f"{'config':<16}"
        + "".join(f"R@{k:<6}" for k in KS)
        + "".join(f"nDCG@{k:<4}" for k in KS)
        + "".join(f"CP@{k:<5}" for k in KS)
        + "MRR"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        line = f"{r.config:<16}"
        line += "".join(f"{r.recall[k]:<8.3f}" for k in KS)
        line += "".join(f"{r.ndcg[k]:<9.3f}" for k in KS)
        line += "".join(f"{r.cp[k]:<8.3f}" for k in KS)
        line += f"{r.mrr:.3f}"
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rag-service retrieval eval harness")
    parser.add_argument(
        "--golden", type=Path, default=Path(__file__).resolve().parent / "golden_kb.json",
        help="Path to the golden KB JSON.",
    )
    parser.add_argument("--json", action="store_true", help="Emit metrics as JSON.")
    parser.add_argument(
        "--assert-hybrid-ge-dense", action="store_true",
        help="Exit non-zero if hybrid's mean nDCG@5 is below dense's (regression gate).",
    )
    parser.add_argument(
        "--assert-rerank-precision", action="store_true",
        help="Exit non-zero if hybrid+rerank's mean Context-Precision@5 is below hybrid's.",
    )
    args = parser.parse_args(argv)

    golden = _load_golden(args.golden)
    rows = asyncio.run(evaluate(golden, configs=_configs_for(golden)))

    if args.json:
        print(json.dumps(
            [
                {"config": r.config, "recall": r.recall, "ndcg": r.ndcg,
                 "context_precision": r.cp, "mrr": r.mrr}
                for r in rows
            ],
            indent=2,
        ))
    else:
        _print_table(rows)

    by = {r.config: r for r in rows}
    if args.assert_hybrid_ge_dense:
        dense_ndcg5 = by["dense"].ndcg[5]
        hybrid_ndcg5 = by["hybrid"].ndcg[5]
        if hybrid_ndcg5 + 1e-9 < dense_ndcg5:
            print(
                f"REGRESSION: hybrid nDCG@5 {hybrid_ndcg5:.4f} < dense {dense_ndcg5:.4f}",
                file=sys.stderr,
            )
            return 1
    if args.assert_rerank_precision:
        hybrid_cp5 = by["hybrid"].cp[5]
        rerank_cp5 = by["hybrid+rerank"].cp[5]
        if rerank_cp5 + 1e-9 < hybrid_cp5:
            print(
                f"REGRESSION: hybrid+rerank CP@5 {rerank_cp5:.4f} < hybrid {hybrid_cp5:.4f}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
