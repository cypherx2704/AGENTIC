# Phase A — confidence-weighted, bitemporally-auditable retrieval (IMPLEMENTED)

> Surgical upgrade of cypherx-a1's edge layer + hybrid retrieval: an explicit `supersedes_edge_id` contradiction chain (Zep/Graphiti bi-temporal invalidation), a single extraction confidence floor (flag-or-drop), and a graph-aware rerank that folds edge confidence × recency onto the fused RRF score (MemGPT precedence, adapted). No new DB, service, engine swap, or contract change — additive only, and verified live against real Postgres, over HTTP, and through the MCP server.

## 1. Scope & rationale

A 5-agent research pass over 38 works concluded cypherx-a1's design is already at 2024–2025 SOTA for temporal-graph agent memory, so the wins are **surgical**. Phase A cashes three of them without touching the architecture:

| Win | Grounding | What it buys |
| --- | --- | --- |
| Explicit supersede link between a closed edge and its replacement | Zep/Graphiti bi-temporal invalidation (Rasmussen 2025) | A contradiction (ownership reassigned, a relation revised when its source artifact changes) is auditable as a **chain**, not an orphaned `valid_to` close. |
| Extraction confidence floor (one threshold, flag-or-drop) | extraction discipline; preserves recall by default | Speculative LLM edges are quarantined (or dropped) without silently entering the crown-jewel graph at full weight. |
| Graph-aware rerank: confidence × recency on the fused RRF score | MemGPT precedence (2023), **adapted** — we fold precedence into the existing RRF rerank, NOT MemGPT's memory-tiering | High-confidence **current** edges outrank speculative/stale ones; freshness is a tunable, disable-able term. |

What Phase A is **not**: it does not add a graph engine, a community-detection / CPG layer, a cross-encoder reranker, an LLM-judge rerank, or any RRF parameter sweep. The bi-temporal invalidation reuses the existing `valid_to` mechanism; the rerank reuses the existing RRF fusion; the floor is **one** number. The LLM never self-edits the shared graph — extraction proposes, the floor gates, and the writer applies deterministically.

Owning files (all on `development`):

| Concern | File / function |
| --- | --- |
| Migration (additive column + index) | `db/migrations/20260614_0003__phaseA.sql` |
| Config knobs | `src/cypherx_a1/core/config.py` (`Settings`) |
| Confidence floor | `src/cypherx_a1/extraction/extractor.py` → `_parse_edges`, threaded through `_extract_node` |
| Supersede chain writer | `src/cypherx_a1/db/graph_repo.py` → `upsert_extracted_edge` |
| Confidence/recency surfacing | `src/cypherx_a1/db/graph_repo.py` → `find_entities`, `keyword_search` |
| Graph-aware rerank | `src/cypherx_a1/retrieval/orchestrator.py` → `rerank_multiplier`, `RetrievalOrchestrator.retrieve`, `_entity_item` |
| Unit tests | `tests/test_phase_a.py` |
| Live DB verification | `scripts/live_graph_demo.py` |

---

## 2. The `supersedes_edge_id` migration + `upsert_extracted_edge` chain

### 2.1 Migration (additive, idempotent)

`db/migrations/20260614_0003__phaseA.sql` adds one nullable column and one partial index — no enum change, no RLS change, no rewrite of existing rows:

```sql
ALTER TABLE cypherx_a1.edges
  ADD COLUMN IF NOT EXISTS supersedes_edge_id UUID;

CREATE INDEX IF NOT EXISTS idx_edges_supersedes
  ON cypherx_a1.edges (tenant_id, supersedes_edge_id) WHERE supersedes_edge_id IS NOT NULL;
```

`supersedes_edge_id` is a self-reference: a **new** edge points back at the **closed** edge it replaced. The partial index (`WHERE supersedes_edge_id IS NOT NULL`) is the walk path for a supersede chain within a tenant, and stays empty-cheap because the column is null on the overwhelming majority of edges. The `cxa1_user` runtime role keeps its `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` idempotency; re-running the migration is a no-op.

This composes with the existing edge bi-temporality already in `20260614_0001__init.sql`: `edges` carries `confidence NUMERIC(4,3)`, `valid_from`, `valid_to` (NULL ⇒ current), `extractor_version`, and `metadata JSONB`. Phase A adds the **link**; the close mechanism (`valid_to`) was already there.

### 2.2 The write chain — `graph_repo.upsert_extracted_edge`

`upsert_extracted_edge(conn, *, src_entity_id, dst_entity_id, rel, confidence, extractor_version, metadata)` is the bi-temporal upsert used by the extractor for **LLM-extracted** edges (the deterministic ingest path keeps `upsert_edge`, which supersedes-in-place without a link). Its contract:

1. **Find the current edge** for `(src_entity_id, dst_entity_id, rel)` where `valid_to IS NULL`.
2. **No-change short-circuit.** If a current edge exists and neither its confidence (within `1e-3`) nor its `metadata` changed, return its `edge_id` untouched. Re-extraction of identical content writes nothing — idempotent, no churn, no spurious chain.
3. **Content change ⇒ close + link.** If the current edge's confidence or metadata materially changed, set `valid_to = NOW()` on the old edge, then INSERT the new edge with `supersedes_edge_id = <old edge_id>`. The result is an auditable contradiction chain: `new.supersedes_edge_id → old.edge_id`, and `old.valid_to` is set.
4. **No prior edge ⇒ fresh INSERT** with `supersedes_edge_id = NULL`.

`tenant_id` is supplied by the RLS context (`NULLIF(current_setting('app.tenant_id', true), '')::uuid`), never from the call body — the function assumes it is already inside an `in_tenant` transaction (per the `graph_repo` invariant: the repo never opens its own tx or sets `app.tenant_id`).

The extractor wires this together in `extraction/extractor.py::_extract_node`: per node it first calls `graph_repo.supersede_extracted_edges(conn, src_entity_id, extractor_version=ev)` (bi-temporally closing prior extracted edges from that node whose `extractor_version` differs and is not `'ingest'`, so a model/prompt bump supersedes rather than duplicates), resolves/creates the target entity, then calls `upsert_extracted_edge` for each surviving edge. Below-floor edges that were kept (flag mode) carry `metadata.flagged = true`.

### 2.3 What "auditable" means concretely

Given an ownership reassignment or a revised extracted relation, the graph retains both the old (now `valid_to`-closed) edge and the new edge, joined by `supersedes_edge_id`. A reader can reconstruct the full revision history of any `(src, dst, rel)` by walking the chain — the platform never loses *why the answer changed*, which is the whole point of bi-temporal engineering memory.

---

## 3. The extraction confidence floor (flag / drop)

The LLM extractor (`response_format=json_object`) can emit speculative edges. Phase A gates them with **one** floor and **one** mode — deliberately not a per-relation tier or a learned threshold.

Config (`core/config.py`):

| Key | Default | Meaning |
| --- | --- | --- |
| `extraction_confidence_floor` | `0.6` | Edges with `confidence < floor` are below the floor. |
| `confidence_floor_mode` | `"flag"` | `"flag"` keeps the edge but marks it (recall-preserving, default); `"drop"` excludes it entirely (tighter graph). |

Enforcement lives in `extraction/extractor.py::_parse_edges(content, *, floor, mode)`, called from `_extract_node` with `floor=settings.extraction_confidence_floor, mode=settings.confidence_floor_mode`:

- The parser is tolerant — a non-JSON / malformed / wrong-shaped response yields `[]` (and the job is still recorded, so it is not retried forever).
- Each edge's `confidence` is clamped to `[0.0, 1.0]`; a missing/garbage value defaults to `0.5`.
- `flagged = confidence < floor`. In `drop` mode a flagged edge is skipped (`continue`); in `flag` mode it is kept with `flagged: True`.
- A kept-but-flagged edge is persisted with `metadata.flagged = true` by `_extract_node`, so downstream readers (or a future review queue) can filter on it without losing the recall.

This is the **only** confidence threshold Phase A introduces (the consolidation threshold in Phase B is separate and singular by the same guardrail). No relation-specific floors, no sweep.

---

## 4. Graph-aware rerank

### 4.1 The formula

After the three-leg RRF fusion (graph + RAG-dense + tsvector keyword) in `RetrievalOrchestrator.retrieve`, every fused item's score is multiplied by a graph-aware factor computed in `retrieval/orchestrator.py::rerank_multiplier`:

```
rerank_multiplier(confidence, created_at) =
    (1 + w_conf * confidence) * ((1 - w_rec) + w_rec * recency)

recency = 0.5 ** (age_days / halflife)      # age_days from created_at to now
```

Properties (each asserted by a unit test):

- **Confidence dominance.** A high-confidence current edge beats a low-confidence one at equal recency (the `(1 + w_conf·confidence)` term).
- **Recency tie-break.** Same confidence, fresher wins (the half-life decay).
- **Disable-able time term.** `w_rec = 0` ⇒ the recency factor collapses to `1.0`; age is ignored.
- **Null-safe.** `created_at is None` (e.g. a RAG chunk with no edge) ⇒ `recency = 1.0`, no crash. Chunks default to `confidence = 1.0`, so the multiplier is recency-neutral for them and they are ranked on RRF alone.
- **Pure + unit-testable.** `rerank_multiplier` takes `now`, `w_conf`, `w_rec`, `halflife` as explicit args — no clock or settings dependency inside the function.

Config (`core/config.py`):

| Key | Default | Role |
| --- | --- | --- |
| `rerank_confidence_weight` (`w_conf`) | `1.0` | Weight on edge confidence. |
| `rerank_recency_weight` (`w_rec`) | `0.5` | Weight on the recency-decay term; `0` disables time. |
| `rerank_recency_halflife_days` (`halflife`) | `90.0` | Half-life of the `0.5 ** (age_days / halflife)` decay (clamped to `≥ 1.0`). |

The orchestrator reads `now = datetime.now(UTC)` once per retrieval and applies the multiplier to each item's `rrf_score` before the final `sorted(..., reverse=True)` and `[: retrieval_context_max_chunks]` truncation. This is the *only* rerank — no cross-encoder, no LLM judge.

### 4.2 Surfacing the signals — `find_entities` / `keyword_search`

The multiplier needs two per-entity signals: the strongest **current** edge confidence touching the entity, and the entity's age. Both graph legs now surface them.

`graph_repo.find_entities` and `graph_repo.keyword_search` each select `created_at` plus a correlated subquery for the strongest current edge confidence:

```sql
COALESCE((SELECT max(ed.confidence) FROM cypherx_a1.edges ed
           WHERE (ed.src_entity_id = entities.entity_id
                  OR ed.dst_entity_id = entities.entity_id)
             AND ed.valid_to IS NULL), 1.0) AS edge_confidence
```

`COALESCE(..., 1.0)` makes an entity with no edges confidence-neutral (it ranks on RRF alone, not penalized). The subquery is scoped to the **current** slice (`valid_to IS NULL`) so a superseded edge never inflates the rerank — exactly the bi-temporal discipline Phase A's chain enforces on the write side.

`retrieval/orchestrator.py::_entity_item` maps these into the `EvidenceItem`:

```python
confidence=float(row.get("edge_confidence") or 1.0),
created_at=row.get("created_at"),
```

`EvidenceItem` defaults `confidence = 1.0` / `created_at = None` so chunk items and any leg that doesn't carry the fields degrade gracefully to a recency-neutral, confidence-neutral multiplier of `(1 + w_conf)`.

---

## 5. Live-verification evidence

Phase A is verified at three layers: pure unit tests, a real-Postgres live demo, and (alongside Phase B) over HTTP + MCP.

### 5.1 Unit tests — `tests/test_phase_a.py` (network-free)

| Test | Asserts |
| --- | --- |
| `test_confidence_floor_flag_mode_keeps_but_flags` | floor `0.6`, mode `flag` ⇒ both edges kept; `0.9` edge `flagged=False`, `0.3` edge `flagged=True`. |
| `test_confidence_floor_drop_mode_removes_low` | floor `0.6`, mode `drop` ⇒ only the `0.9` edge survives. |
| `test_parse_edges_tolerant_of_garbage` | `None`, `"not json"`, `{"edges":"nope"}` all ⇒ `[]`. |
| `test_rerank_high_confidence_recent_outranks_low_stale` | hi-conf recent > low-conf stale; hi-conf > low-conf at equal recency; fresher > stale at equal confidence. |
| `test_rerank_recency_weight_zero_ignores_age` | `w_rec=0` ⇒ identical multiplier for a 999-day-old and a now edge. |
| `test_rerank_handles_missing_created_at` | `created_at=None` ⇒ positive multiplier, no crash. |

### 5.2 Live DB demo — `scripts/live_graph_demo.py` (real Postgres, RLS-enforced `cxa1_user`)

The demo ingests the GitHub fixtures through the real normalizer (graph-only), then exercises the actual Phase A write path inside an `in_tenant` transaction:

```python
e1 = await graph_repo.upsert_extracted_edge(conn, src_entity_id=alice, dst_entity_id=svc,
                                             rel="expert_in", confidence=0.6,  extractor_version="demo")
e2 = await graph_repo.upsert_extracted_edge(conn, src_entity_id=alice, dst_entity_id=svc,
                                             rel="expert_in", confidence=0.95, extractor_version="demo")
# assert: e2.supersedes_edge_id == e1   AND   e1.valid_to IS NOT NULL
```

It prints the verdict:

```
=== Phase A supersede chain: new edge -> old link=True, old edge closed=True -> PASS ===
```

i.e. the confidence change `0.6 → 0.95` closed the prior `expert_in` edge (`e1.valid_to` set) and linked the replacement (`e2.supersedes_edge_id == e1`). The same script also confirms the cross-tenant RLS isolation that Phase A's queries inherit (tenant B sees `0/0`), so the new correlated-subquery surfacing does not leak across tenants.

### 5.3 Status

Phase A is **IMPLEMENTED and verified** on `development`: migration applied, floor enforced in extraction, rerank live in the orchestrator, supersede chain PASS against real Postgres, all `tests/test_phase_a.py` cases green. The copilot HTTP path (`POST /v1/copilot/ask`) and the MCP server (`mcp-eng-memory`) consume the reranked retrieval transparently — no API/contract surface changed.

---

## 6. Over-engineering avoided (guardrails honored)

Phase A holds the line set by the research verdict's **SKIP** list and the project guardrails:

- **No new DB / service / engine swap.** One additive column + index on the existing `cypherx_a1.edges`; still the adjacency-list + recursive-CTE graph on frozen `pgvector/pg16` (no Apache AGE/Neo4j, no `CREATE EXTENSION`).
- **No community detection / Leiden / CPG.** The supersede chain is a single self-link, not a graph-rewrite.
- **No LLM-judge / RankGPT rerank, no cross-encoder.** The rerank is a closed-form `confidence × recency` multiplier on the existing RRF score.
- **No RRF parameter sweep.** `retrieval_rrf_k` stays at the canonical `60`; the rerank is layered on top, not a re-tuning of fusion.
- **One floor, one rerank.** A single `extraction_confidence_floor` with a single `confidence_floor_mode`; no per-relation tiers.
- **The LLM never self-edits the shared graph.** Extraction proposes edges; `_parse_edges` gates them at the floor; `upsert_extracted_edge` applies them deterministically with an auditable chain.
- **Nothing pushed into SharedCore, no contract broken.** All changes are app-owned and internal to cypherx-a1; the consumed `/v1` RAG contract and the MCP manifest are untouched.

Deferred precedence ideas that Phase A deliberately did **not** adopt: MemGPT's memory **tiering** (we took only its precedence signal into the rerank), and any evidence-gated reranker — those remain Phase C/D, to be built only on a measured trigger.

---

## 7. Forward plans (context, not built in Phase A)

These are documented elsewhere and are **not** part of Phase A. Phase B is implemented alongside Phase A; C and D are forward plans.

| Phase | Status | Headline |
| --- | --- | --- |
| **B** | IMPLEMENTED + verified | Activity/change surface (commit-level `change` nodes, `activity_timeline`, `POST /v1/graph/activity`, MCP `what_changed`) + reflection/consolidation pass (`consolidator.run_consolidation` → `expertise_summary` nodes, graph-only, idempotent, cost-metered). |
| **C** | Documented, not implemented | Recency-decayed Degree-of-Knowledge `expert_in` + ownership-concentration; a lightweight regex query-type classifier → per-leg RRF weights; semantic identity resolution + human-review queue on top of exact handle/email match. |
| **D** | Documented, evidence-gated | Build **only** on a measured trigger: SZZ change→cause attribution into `caused` edges; single-hop Personalized-PageRank reviewer recommendation; RAPTOR-L2 meta-consolidation for >1k-edge tenants. |
