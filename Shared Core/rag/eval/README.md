# rag-service retrieval eval harness

A tiny, infra-free harness that measures retrieval quality across the search configurations the
service supports, so the hybrid + rerank + query-transformation upgrades are **measurable** and
guarded against regression.

It runs entirely against the in-process fakes (`tests/fakes.py`) + the deterministic mock
embedder + mock reranker — **no Postgres, llms-gateway, Kafka, or network**.

## Files

| File | Purpose |
|------|---------|
| `golden_kb.json` | Golden KB: one chunk per document + queries annotated with their relevant chunk ids. Base slice = mixed verbatim-lexical + paraphrase queries; `multihop` slice = MultiHop-RAG-style compound queries whose facts live in separate docs (B2); `multiquery` slice = vocabulary-mismatch queries with hand-authored variants (B3). |
| `run_eval.py` | Loads the golden KB, indexes each slice with the mock embedder, runs `dense` / `hybrid` / `hybrid+rerank` (base slice) + `decompose` / `multi_query` (their own slices), and reports recall@k, nDCG@k, Context-Precision@k, MRR. |

## Run

```bash
uv run python -m eval.run_eval            # or: .venv/Scripts/python.exe eval/run_eval.py
uv run python -m eval.run_eval --json     # machine-readable metrics
uv run python -m eval.run_eval --assert-hybrid-ge-dense    # regression gate: hybrid nDCG@5 ≥ dense
uv run python -m eval.run_eval --assert-rerank-precision   # regression gate: rerank CP@5 ≥ hybrid
```

## Metrics

- **recall@k** — fraction of a query's relevant chunks present in the top-k.
- **nDCG@k** — rank-discounted gain (binary relevance), normalized to the ideal ordering.
- **CP@k** — RAGAS-style ID-based **Context Precision** (mean Precision@i over the ranks a
  relevant chunk appears): pure arithmetic over the golden relevant-id list, no LLM. Isolates
  *window precision* (how many distractors sit among the relevant chunks the caller's LLM reads)
  — the signal recall@k (blind to non-relevant placement) and nDCG@k (ideal-normalized) miss.
  Collapses to MRR on single-relevant queries; genuinely distinct on multi-relevant ones.
- **MRR** — reciprocal rank of the first relevant chunk.

All metrics are averaged over the query set and reported at k ∈ {1, 3, 5, 10}.

## What it shows

With the deterministic mock embedder (semantically meaningless vectors, used so the harness
needs no model/network), the **dense** baseline is weak, the **lexical/RRF hybrid** leg
recovers most relevant chunks, and the **rerank** stage sharpens the top of the list. The
`decompose` config (measured on the `multihop` slice) co-retrieves facts scattered across
separate docs (recall@3 = 1.0); `multi_query` (measured on the `multiquery` slice) recovers
vocabulary-mismatch misses via RRF fusion (a strict recall lift over single-query).

```
config          R@1     R@3     R@5     R@10    nDCG@1   nDCG@3   nDCG@5   nDCG@10  CP@1    CP@3    CP@5    CP@10   MRR
dense           0.095   0.238   0.238   0.690   0.143    0.195    0.195    0.339    0.143   0.214   0.214   0.243   0.269
hybrid          0.488   0.798   0.905   0.952   0.643    0.735    0.784    0.807    0.643   0.774   0.792   0.786   0.792
hybrid+rerank   0.762   0.952   0.952   0.952   1.000    0.962    0.962    0.962    1.000   1.000   1.000   1.000   1.000
decompose       0.500   1.000   1.000   1.000   1.000    0.960    0.960    0.960    1.000   0.917   0.917   0.917   1.000
multi_query     0.000   0.500   0.500   1.000   0.000    0.387    0.387    0.571    0.000   0.500   0.500   0.361   0.500
```

(Exact numbers are deterministic but depend on the mock vectors; the ordering
`dense ≤ hybrid ≤ hybrid+rerank` on nDCG@5 / CP@5 is what the harness asserts.) Against a
**real** embedding model the dense baseline is far stronger; hybrid + rerank still add
lexical-exactness and top-of-list precision — this harness lets you re-measure the delta
whenever the retrieval path changes. `tests/test_eval_harness.py` + `tests/test_eval_metrics.py`
run the same harness as CI regression gates.

### Measurement caveat (honest)

The mock embedder is **non-semantic**, so it cannot demonstrate the *semantic* benefit of the
query-transformation features. `decompose` therefore shows that decomposition **co-retrieves**
scattered facts and never regresses vs single-query (a strict retrieval lift needs a real
embedder — the mock's lexical leg finds a chunk whenever its distinctive tokens appear in the
query, and each sub-question's tokens are a substring of the compound query). `multi_query` *can*
show a strict recall lift because its variants are **hand-authored** with the relevant chunks'
vocabulary (which the original wording lacks), so the lexical leg — not the mock vectors —
carries the signal.
