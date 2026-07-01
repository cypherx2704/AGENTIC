# eval/ — rerank + safety-classify quality harness

A tiny, self-contained, keyless harness that makes the quality of the `/v1/rerank` and
`/v1/classify` providers **measurable** — so the improvement from the deterministic
default (mock reranker / stub classifier) to a real model (cross-encoder behind
`RERANK_PROVIDER=local`, safety model behind `CLASSIFIER_MODE=local`) is a number, not a
vibe. No DB, no network, no keys: it imports the providers directly and runs them over a
golden set.

## Files

| File | Holds |
|------|-------|
| `golden_rerank.jsonl` | Rerank golden set: `{query, documents:[{id,text,relevant}]}` per line. `relevant` (0/1) is the human relevance label used to score ranking quality. |
| `golden_classify.jsonl` | Classify golden set: `{input, direction, expect_verdict, expect_categories}` per line. |
| `run_eval.py` | Metric runner. Reranks/classifies each golden case with the SELECTED provider and prints the metrics. Exit code is non-zero if a metric falls below its threshold (CI gate). |

## Metrics

* **Rerank** — `NDCG@k`, `MRR`, and `Recall@k` over the relevance labels (default k=3).
  NDCG rewards putting the relevant docs at the top; MRR is the reciprocal rank of the
  first relevant doc. A better reranker raises all three.
* **Classify** — verdict **accuracy** plus per-direction breakdown and a category
  precision/recall against the expected categories.

## Run

```bash
# Default deterministic providers (RERANK_PROVIDER=mock, CLASSIFIER_MODE=stub):
python -m uv run python eval/run_eval.py

# Measure a real model once provisioned (the seam returns 503 until then):
RERANK_PROVIDER=local CLASSIFIER_MODE=local python -m uv run python eval/run_eval.py

# Custom golden sets / k / thresholds:
python -m uv run python eval/run_eval.py --rerank-golden eval/golden_rerank.jsonl --k 3 \
  --min-ndcg 0.7 --min-verdict-accuracy 0.8
```

The harness is offline + deterministic, so the same golden set + provider always yields
the same numbers — a regression in either provider shows up as a metric drop.
