# Memory retrieval eval harness

A small, offline golden-set + metric runner that **measures** the retrieval-scoring
improvement: it compares the default **pure-cosine** order against the flagged
**composite** order (recency + importance + relevance, the Stanford "Generative Agents"
score) on the same candidate set.

This is deliberately tiny and dependency-free (no network, no heavy model): a deterministic
bag-of-words embedder gives semantically-related text a high cosine so recall@k is
meaningful, and the golden set encodes the cases composite scoring is supposed to win —
an *important* or *recent* memory that pure cosine would rank just below a slightly-closer
but stale/trivial one.

## What it measures

* **recall@k** — fraction of golden queries whose expected memory id is in the top-k.
* **helpfulness proxy** — average normalized rank of the expected memory (1.0 = always
  first; lower = buried). A cheap stand-in for "did the agent get the memory it needed".

## Files

| File | Purpose |
|------|---------|
| `golden_memories.json` | The golden set: a corpus of memories + queries with the expected memory id and notes on why composite should help. |
| `run_eval.py` | The runner. Loads the golden set, stores it in the in-memory repo, runs each query under both rankings, prints a comparison table + a `PASS/FAIL` on "composite ≥ cosine". |

## Run

```bash
./.venv/Scripts/python.exe eval/run_eval.py            # human-readable table
./.venv/Scripts/python.exe eval/run_eval.py --json     # machine-readable metrics
```

Exit code is non-zero if composite scoring regresses recall@k vs cosine on the golden set,
so this doubles as a guardrail in CI. `tests/test_eval_harness.py` runs the same harness
and asserts composite does not regress.
