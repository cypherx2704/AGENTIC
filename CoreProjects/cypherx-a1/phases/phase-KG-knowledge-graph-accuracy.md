# Phase KG — knowledge-graph ACCURACY (IMPLEMENTED)

> App-side accuracy upgrade of cypherx-a1's knowledge graph, grounded in temporal-KG
> (Zep/Graphiti bi-temporal invalidation), coreference (LINK-KG), and schema-guided / ontology-
> constrained extraction. FOUR additive wins — true bi-temporal edges, type-aware entity
> resolution, schema-guided extraction, and per-edge extraction QA — plus a small reusable
> `kg/` lib so a future shared service is a lift-out, not a rewrite. **The graph stays APP-
> OWNED**; nothing is pushed into SharedCore. ADDITIVE only — no new pg extension, no enum
> removal, no RLS removal, no contract change, and every flag defaults to today's behavior.

## 1. Scope

| Win | Grounding | What it buys |
| --- | --- | --- |
| **Bi-temporal edges** (`valid_until` / `ingested_at` / `invalidated_at`) | Zep/Graphiti bi-temporal invalidation | A contradiction (same subject+relation) sets the old edge's fact-time end + ingest-time invalidation stamp instead of deleting; default reads stay on the current slice, with additive **as-of / history** reads. |
| **Entity resolution / canonicalization** | LINK-KG coreference | A mention→canonical map + TYPE-AWARE coreference ('J. Smith' / 'John Smith' → one entity); edges redirect to the canonical id; mentions preserved for audit. |
| **Schema/ontology-guided extraction** | ontology-constrained extraction (vs open IE) | The extractor is constrained to an allowed relation/entity-type set; off-schema (hallucinated) relations are rejected or flagged. |
| **Extraction QA** | extraction discipline | Per-edge `source_span` + `extraction_confidence`; below-floor facts dropped/flagged (reuses Phase A's floor). |
| **Reusable `kg/` lib** | lift-out, not rewrite | Pure schema + resolution + extraction logic, no DB/LLM/settings — a shared KG service can adopt it directly. |

All five honor the project guardrails: pure SQL / recursive-CTE, adjacency-list, **no
`CREATE EXTENSION`**, outbox-no-RLS untouched, identity from JWT only.

## 2. Files

| Concern | File |
| --- | --- |
| Migration (additive columns + `entity_mentions` table + RLS + grants) | `db/migrations/20260614_0005__phaseKG.sql` |
| Reusable lib — ontology / schema-guided extraction | `src/cypherx_a1/kg/schema.py` |
| Reusable lib — type-aware coreference | `src/cypherx_a1/kg/resolution.py` |
| Reusable lib — schema-constrained, QA-gated parsing | `src/cypherx_a1/kg/extraction.py` |
| DB-backed resolver (wires the pure logic to the graph) | `src/cypherx_a1/ingestion/resolver.py` |
| Normalizer hook (opt-in resolution) | `src/cypherx_a1/ingestion/normalizer.py` |
| Extractor (schema + span/confidence QA) | `src/cypherx_a1/extraction/extractor.py` |
| Bitemporal writes + as-of/history reads + mention/merge ops | `src/cypherx_a1/db/graph_repo.py` |
| As-of query param (additive) | `src/cypherx_a1/copilot/queries.py`, `api/graph.py`, `models/api.py` |
| Config flags | `src/cypherx_a1/core/config.py` |
| Metrics | `src/cypherx_a1/core/metrics.py` |
| Eval harness (golden set + metric runner) | `eval/run_eval.py`, `eval/golden/*.json` |
| Unit tests | `tests/test_phase_kg.py`, `tests/test_eval_harness.py` |
| Live DB verification | `scripts/live_graph_demo.py` (Phase KG block) |

## 3. Flags (ALL default to today's behavior)

| Key | Default | Effect when set |
| --- | --- | --- |
| `extraction_schema_enabled` | `false` | Constrain the extractor to `kg.DEFAULT_SCHEMA`. |
| `extraction_schema_mode` | `reject` | `reject` drops off-schema relations; `flag` keeps them with `metadata.schema_ok=false`. |
| `extraction_span_capture_enabled` | `false` | Populate `edges.source_span` + `edges.extraction_confidence`. |
| `entity_resolution_enabled` | `false` | Run type-aware coreference + merge on ingest. |
| `entity_resolution_min_confidence` | `0.85` | Min coref confidence to auto-merge. |

With every flag off, normalization + extraction are byte-for-byte today's behavior, and the
new columns stay NULL — the verified spine + MVP tests are unchanged.

## 4. Migration detail

`20260614_0005__phaseKG.sql` is additive + idempotent:
- `edges += valid_until, ingested_at (default NOW), invalidated_at, source_span, extraction_confidence` (all nullable/defaulted) + a partial `idx_edges_valid_until` for as-of scans.
- `entity_mentions` (mention→canonical map) with RLS enabled+forced (Contract 13) and the canonical isolation policy, plus `cxa1_user` grants. Unique on `(tenant_id, kind, normalized_form)`.
- No enum change, no extension, no rewrite of existing rows.

The existing `valid_from`/`valid_to` mechanism is the live close path; `valid_until` mirrors
the fact-time end and `invalidated_at` records the system-time invalidation, so a contradiction
is bi-temporally auditable (`graph_repo.edge_history` walks the full chain).

## 5. Eval

`python eval/run_eval.py` scores the `kg/` lib against `eval/golden/`:

```
schema_rejection_precision  1.000  (target >= 1.00)  [PASS]   # no hallucinated relation leaks
schema_recall               1.000  (target >= 1.00)  [PASS]   # no in-schema over-rejection
floor_accuracy              1.000  (target >= 1.00)  [PASS]
coref_accuracy              1.000  (target >= 1.00)  [PASS]
```

`tests/test_eval_harness.py` regression-gates these metrics in CI.

## 6. Over-engineering avoided

- No new graph engine / pg extension / heavy infra — pure SQL + recursive CTE on the frozen image.
- Coreference is CONSERVATIVE + type-aware (keyed kinds never fuzzy-merge; nicknames are not auto-merged without a table) — a wrong merge is worse than a missed one.
- The LLM never self-edits the graph: extraction proposes, the schema + floor gate, the writer applies deterministically.
- One reusable lib, pure logic only; the DB + identity + RLS stay app-owned (graph is the crown jewel, NOT pushed into SharedCore).
