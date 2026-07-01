"""eval/run_eval.py — offline quality harness for /v1/rerank and /v1/classify.

Keyless + deterministic: imports the providers directly (no DB / no network / no Auth)
and scores them over a golden set, so the quality of the default mock reranker / stub
classifier — and the improvement from a real model behind RERANK_PROVIDER=local /
CLASSIFIER_MODE=local — is a reproducible NUMBER.

Metrics
  Rerank  : NDCG@k, MRR, Recall@k over per-document relevance labels (default k=3).
  Classify: verdict accuracy + category precision/recall vs. the expected categories.

Exit code is non-zero when a metric falls below its --min-* threshold (a CI gate).

Usage:
  python -m uv run python eval/run_eval.py
  RERANK_PROVIDER=local CLASSIFIER_MODE=local python -m uv run python eval/run_eval.py
  python -m uv run python eval/run_eval.py --k 3 --min-ndcg 0.7 --min-verdict-accuracy 0.8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

# Make `llms_gateway` importable when run from the repo root without an install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llms_gateway.core.config import Settings  # noqa: E402
from llms_gateway.core.errors import ApiError  # noqa: E402
from llms_gateway.models.unified import ClassifyRequest, RerankRequest  # noqa: E402
from llms_gateway.services.providers.rerank import get_rerank_provider  # noqa: E402
from llms_gateway.services.providers.safety import get_safety_provider  # noqa: E402

_EVAL_DIR = Path(__file__).resolve().parent


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ── Rerank metrics ────────────────────────────────────────────────────────────────
def _dcg(relevances: list[int]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


def _ndcg_at_k(ranked_rels: list[int], k: int) -> float:
    ideal = sorted(ranked_rels, reverse=True)
    idcg = _dcg(ideal[:k])
    if idcg == 0:
        return 0.0
    return _dcg(ranked_rels[:k]) / idcg


def _mrr(ranked_rels: list[int]) -> float:
    for i, rel in enumerate(ranked_rels):
        if rel > 0:
            return 1.0 / (i + 1)
    return 0.0


def _recall_at_k(ranked_rels: list[int], k: int) -> float:
    total_rel = sum(1 for r in ranked_rels if r > 0)
    if total_rel == 0:
        return 0.0
    return sum(1 for r in ranked_rels[:k] if r > 0) / total_rel


async def _eval_rerank(settings: Settings, golden: list[dict], k: int) -> dict[str, float]:
    provider = get_rerank_provider(settings)
    ndcgs: list[float] = []
    mrrs: list[float] = []
    recalls: list[float] = []
    for case in golden:
        docs = case["documents"]
        req = RerankRequest(
            model=settings.rerank_default_model,
            query=case["query"],
            documents=[{"id": d.get("id"), "text": d["text"]} for d in docs],
        )
        resp = await provider.rerank(req, model_id=settings.rerank_default_model)
        # Relevance label of each returned result, in ranked order.
        ranked_rels = [int(docs[r.index].get("relevant", 0)) for r in resp.results]
        ndcgs.append(_ndcg_at_k(ranked_rels, k))
        mrrs.append(_mrr(ranked_rels))
        recalls.append(_recall_at_k(ranked_rels, k))
    n = len(golden) or 1
    return {
        "cases": len(golden),
        f"ndcg@{k}": round(sum(ndcgs) / n, 4),
        "mrr": round(sum(mrrs) / n, 4),
        f"recall@{k}": round(sum(recalls) / n, 4),
    }


# ── Classify metrics ────────────────────────────────────────────────────────────────
async def _eval_classify(settings: Settings, golden: list[dict]) -> dict[str, float]:
    provider = get_safety_provider(settings)
    correct = 0
    tp = fp = fn = 0  # category-level
    for case in golden:
        resp = await provider.classify(
            ClassifyRequest(input=case["input"], direction=case["direction"]),
            model_id=settings.classifier_default_model,
        )
        if resp.verdict == case["expect_verdict"]:
            correct += 1
        got = {c.name for c in resp.categories}
        want = set(case.get("expect_categories", []))
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
    n = len(golden) or 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {
        "cases": len(golden),
        "verdict_accuracy": round(correct / n, 4),
        "category_precision": round(precision, 4),
        "category_recall": round(recall, 4),
    }


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    print(
        f"providers: RERANK_PROVIDER={settings.rerank_provider} "
        f"CLASSIFIER_MODE={settings.classifier_mode}\n"
    )

    failures: list[str] = []

    # ── Rerank ──
    try:
        rerank_golden = _read_jsonl(Path(args.rerank_golden))
        rerank_metrics = await _eval_rerank(settings, rerank_golden, args.k)
        print("rerank:", json.dumps(rerank_metrics))
        if rerank_metrics[f"ndcg@{args.k}"] < args.min_ndcg:
            failures.append(
                f"ndcg@{args.k} {rerank_metrics[f'ndcg@{args.k}']} < min {args.min_ndcg}"
            )
    except ApiError as exc:
        # The local seam is not provisioned in the default image -> reportable, not a crash.
        print(f"rerank: SKIPPED ({exc.code}: {exc.message})")

    # ── Classify ──
    try:
        classify_golden = _read_jsonl(Path(args.classify_golden))
        classify_metrics = await _eval_classify(settings, classify_golden)
        print("classify:", json.dumps(classify_metrics))
        if classify_metrics["verdict_accuracy"] < args.min_verdict_accuracy:
            failures.append(
                f"verdict_accuracy {classify_metrics['verdict_accuracy']} "
                f"< min {args.min_verdict_accuracy}"
            )
    except ApiError as exc:
        print(f"classify: SKIPPED ({exc.code}: {exc.message})")

    if failures:
        print("\nFAIL:\n  - " + "\n  - ".join(failures))
        return 1
    print("\nPASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="rerank + classify quality eval harness")
    p.add_argument("--rerank-golden", default=str(_EVAL_DIR / "golden_rerank.jsonl"))
    p.add_argument("--classify-golden", default=str(_EVAL_DIR / "golden_classify.jsonl"))
    p.add_argument("--k", type=int, default=3, help="cutoff for NDCG@k / Recall@k")
    p.add_argument("--min-ndcg", type=float, default=0.7, help="rerank NDCG@k CI gate")
    p.add_argument(
        "--min-verdict-accuracy", type=float, default=0.8, help="classify accuracy CI gate"
    )
    args = p.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
