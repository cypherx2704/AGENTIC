# cypherx-a1 — knowledge-graph accuracy eval harness

A small, network-free, keyless eval harness that **measures** the Phase KG accuracy wins so
the improvement is quantifiable (not just asserted). It exercises the reusable `kg/` lib
(schema-guided extraction + type-aware coreference + extraction QA) against a golden set and
prints precision / recall / accuracy metrics.

It is deliberately PURE (no Postgres, no LLM, no service tokens) — it scores the decision
logic, which is where the accuracy lives. The DB-backed wiring is covered by the pytest
suite + `scripts/live_graph_demo.py`.

## Run

```bash
python eval/run_eval.py            # human-readable report + a JSON summary
python eval/run_eval.py --json     # JSON only (for CI dashboards)
python -m pytest tests/test_eval_harness.py   # regression-gates the metrics in CI
```

## What it measures

| Metric | Golden set | What it proves |
| --- | --- | --- |
| **Schema-guided extraction** — off-schema rejection precision/recall | `golden/extraction_cases.json` | Hallucinated / out-of-ontology relations are rejected; in-schema relations are kept. |
| **Coreference resolution** — pairwise accuracy | `golden/coref_cases.json` | `'J. Smith'`/`'John Smith'` co-refer; `'John Smith'`/`'Jane Smith'` do not; keyed kinds never over-merge. |
| **Confidence floor** — below-floor handling | `golden/extraction_cases.json` | Low-confidence edges are dropped (drop mode) / flagged (flag mode) as configured. |

## Files

| Path | Holds |
| --- | --- |
| `run_eval.py` | The metric runner (loads the golden set, scores the `kg/` lib, prints the report). |
| `golden/extraction_cases.json` | Labeled extractor completions: which proposed edges are in-schema vs. hallucinated. |
| `golden/coref_cases.json` | Labeled mention pairs: should they co-refer, given their kind. |

Adding a case is additive: append to a golden JSON file and the runner picks it up.
