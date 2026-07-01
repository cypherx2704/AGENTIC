"""The eval harness, exercised as a CI regression gate.

Confirms the harness runs in-process (no infra) and that retrieval quality is monotonic:
dense ≤ hybrid ≤ hybrid+rerank on mean nDCG@5 / MRR over the golden set. This is the
measurable proof that the hybrid + rerank upgrades improve retrieval.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eval.run_eval import evaluate

_GOLDEN = Path(__file__).resolve().parent.parent / "eval" / "golden_kb.json"


@pytest.mark.asyncio
async def test_eval_harness_monotonic_improvement() -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    rows = {r.config: r for r in await evaluate(golden)}
    assert set(rows) == {"dense", "hybrid", "hybrid+rerank"}

    # Hybrid recovers more relevant chunks than dense (lexical leg helps the mock embedder).
    assert rows["hybrid"].ndcg[5] >= rows["dense"].ndcg[5]
    assert rows["hybrid"].recall[5] >= rows["dense"].recall[5]
    # Rerank sharpens the top of the list further (nDCG@5 + MRR).
    assert rows["hybrid+rerank"].ndcg[5] >= rows["hybrid"].ndcg[5]
    assert rows["hybrid+rerank"].mrr >= rows["hybrid"].mrr
    # All metrics are valid fractions.
    for r in rows.values():
        assert 0.0 <= r.mrr <= 1.0
        assert all(0.0 <= r.ndcg[k] <= 1.0 for k in r.ndcg)
        assert all(0.0 <= r.recall[k] <= 1.0 for k in r.recall)
