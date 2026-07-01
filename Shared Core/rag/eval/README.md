# rag-service retrieval eval harness

A tiny, infra-free harness that measures retrieval quality across the three search
configurations the service supports, so the hybrid + rerank upgrades are **measurable** and
guarded against regression.

It runs entirely against the in-process fakes (`tests/fakes.py`) + the deterministic mock
embedder + mock reranker — **no Postgres, llms-gateway, Kafka, or network**.

## Files

| File | Purpose |
|------|---------|
| `golden_kb.json` | Golden KB: one chunk per document + queries annotated with their relevant chunk ids (mixed verbatim-lexical and paraphrase queries). |
| `run_eval.py` | Loads the golden KB, indexes it with the mock embedder, runs `dense` / `hybrid` / `hybrid+rerank`, and reports recall@k, nDCG@k, MRR. |

## Run

```bash
uv run python -m eval.run_eval            # or: .venv/Scripts/python.exe eval/run_eval.py
uv run python -m eval.run_eval --json     # machine-readable metrics
uv run python -m eval.run_eval --assert-hybrid-ge-dense   # regression gate (exit 1 on regress)
```

## Metrics

- **recall@k** — fraction of a query's relevant chunks present in the top-k.
- **nDCG@k** — rank-discounted gain (binary relevance), normalized to the ideal ordering.
- **MRR** — reciprocal rank of the first relevant chunk.

All metrics are averaged over the query set and reported at k ∈ {1, 3, 5}.

## What it shows

With the deterministic mock embedder (semantically meaningless vectors, used so the harness
needs no model/network), the **dense** baseline is weak, the **lexical/RRF hybrid** leg
recovers most relevant chunks, and the **rerank** stage sharpens the top of the list:

```
config           R@1     R@3     R@5     nDCG@1   nDCG@3   nDCG@5   MRR
----------------------------------------------------------------------
dense            0.095   0.238   0.238   0.143    0.195    0.195    0.214
hybrid           0.488   0.798   0.905   0.643    0.735    0.784    0.792
hybrid+rerank    0.762   0.952   0.952   1.000    0.962    0.962    1.000
```

(Exact numbers are deterministic but depend on the mock vectors; the ordering
`dense ≤ hybrid ≤ hybrid+rerank` is what the harness asserts.) Against a **real** embedding
model the dense baseline is far stronger; hybrid + rerank still add lexical-exactness and
top-of-list precision — this harness lets you re-measure the delta whenever the retrieval path
changes. `tests/test_eval_harness.py` runs the same harness as a CI regression gate.
