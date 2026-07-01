"""Retrieval eval harness — recall@k / nDCG@k / MRR for dense vs hybrid vs hybrid+rerank.

Runs ENTIRELY against the in-process fakes (``tests/fakes.py`` FakeDb/FakePool) + the
deterministic mock embedder + mock reranker, so it needs NO Postgres / llms-gateway / network
— ``uv run python -m eval.run_eval`` (or ``.venv/Scripts/python.exe eval/run_eval.py``).

It loads ``eval/golden_kb.json`` (a tiny golden KB: one chunk per doc + queries annotated
with their relevant chunk ids), indexes every doc with the mock embedding (mirroring the real
ingest path's content_tsv via the fake), then evaluates three configurations:

  * ``dense``         — the default two-pass pgvector path (PgVectorAdapter.search).
  * ``hybrid``        — dense + lexical fused with Reciprocal Rank Fusion (search_hybrid).
  * ``hybrid+rerank`` — hybrid candidates re-ordered by the mock cross-encoder reranker.

Metrics (averaged over the query set), reported at k = 1, 3, 5:
  * recall@k — fraction of a query's relevant chunks present in the top-k.
  * nDCG@k   — rank-discounted gain (binary relevance), normalized to the ideal ordering.
  * MRR      — reciprocal rank of the FIRST relevant chunk (rank-cutoff independent).

Exit code is 0 unless ``--assert-hybrid-ge-dense`` is passed AND hybrid fails to match/beat
dense on mean nDCG@5 (a regression gate the test suite can call).
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
from rag_service.services.embeddings import mock_embed  # noqa: E402
from rag_service.services.rerank import mock_rerank  # noqa: E402
from rag_service.services.store.pgvector import PgVectorAdapter  # noqa: E402

TENANT = "00000000-0000-0000-0000-0000000000aa"
KB = "kb-eval"
DIM = 1536
KS = (1, 3, 5)


@dataclass
class MetricRow:
    config: str
    recall: dict[int, float]
    ndcg: dict[int, float]
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


def _mrr(ranked: list[str], relevant: set[str]) -> float:
    for i, cid in enumerate(ranked):
        if cid in relevant:
            return 1.0 / (i + 1)
    return 0.0


async def _rank_dense(adapter: PgVectorAdapter, query: str, *, top_k: int) -> list[str]:
    hits = await adapter.search(
        TENANT, KB, mock_embed([query], DIM)[0],
        top_k=top_k, min_score=-1.0, filters=None, dimension=DIM, ef_search=100,
    )
    return [h.chunk_id for h in hits]


async def _rank_hybrid(adapter: PgVectorAdapter, query: str, *, top_k: int) -> list[str]:
    hits = await adapter.search_hybrid(
        TENANT, KB, mock_embed([query], DIM)[0], query,
        top_k=top_k, candidates=50, rrf_k=60, filters=None, dimension=DIM, ef_search=100,
        mode="hybrid",
    )
    return [h.chunk_id for h in hits]


async def _rank_hybrid_rerank(adapter: PgVectorAdapter, query: str, *, top_k: int) -> list[str]:
    # Retrieve a wider hybrid candidate pool, then re-order with the mock reranker.
    hits = await adapter.search_hybrid(
        TENANT, KB, mock_embed([query], DIM)[0], query,
        top_k=max(top_k, 20), candidates=50, rrf_k=60, filters=None, dimension=DIM, ef_search=100,
        mode="hybrid",
    )
    docs = [h.content for h in hits]
    items = mock_rerank(query, docs, top_n=top_k)
    return [hits[it.index].chunk_id for it in items]


_RANKERS = {
    "dense": _rank_dense,
    "hybrid": _rank_hybrid,
    "hybrid+rerank": _rank_hybrid_rerank,
}


async def evaluate(golden: dict) -> list[MetricRow]:
    db = FakeDb()
    _seed(db, golden["documents"])
    adapter = PgVectorAdapter(FakePool(db), Settings(mock_embeddings=True))
    queries = golden["queries"]
    max_k = max(KS)

    rows: list[MetricRow] = []
    for config, ranker in _RANKERS.items():
        recall = dict.fromkeys(KS, 0.0)
        ndcg = dict.fromkeys(KS, 0.0)
        mrr = 0.0
        for q in queries:
            relevant = set(q["relevant"])
            ranked = await ranker(adapter, q["query"], top_k=max_k)
            for k in KS:
                recall[k] += _recall_at_k(ranked, relevant, k)
                ndcg[k] += _ndcg_at_k(ranked, relevant, k)
            mrr += _mrr(ranked, relevant)
        n = len(queries) or 1
        rows.append(MetricRow(
            config=config,
            recall={k: recall[k] / n for k in KS},
            ndcg={k: ndcg[k] / n for k in KS},
            mrr=mrr / n,
        ))
    return rows


def _print_table(rows: list[MetricRow]) -> None:
    header = (
        f"{'config':<16}"
        + "".join(f"R@{k:<6}" for k in KS)
        + "".join(f"nDCG@{k:<4}" for k in KS)
        + "MRR"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        line = f"{r.config:<16}"
        line += "".join(f"{r.recall[k]:<8.3f}" for k in KS)
        line += "".join(f"{r.ndcg[k]:<9.3f}" for k in KS)
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
    args = parser.parse_args(argv)

    golden = _load_golden(args.golden)
    rows = asyncio.run(evaluate(golden))

    if args.json:
        print(json.dumps(
            [{"config": r.config, "recall": r.recall, "ndcg": r.ndcg, "mrr": r.mrr} for r in rows],
            indent=2,
        ))
    else:
        _print_table(rows)

    if args.assert_hybrid_ge_dense:
        by = {r.config: r for r in rows}
        dense_ndcg5 = by["dense"].ndcg[5]
        hybrid_ndcg5 = by["hybrid"].ndcg[5]
        if hybrid_ndcg5 + 1e-9 < dense_ndcg5:
            print(
                f"REGRESSION: hybrid nDCG@5 {hybrid_ndcg5:.4f} < dense {dense_ndcg5:.4f}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
