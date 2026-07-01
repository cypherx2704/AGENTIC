# Feature list — near-term & future

> The cypherx-a1 (Autonomous Engineering Memory) feature ledger: every enhancement sorted into **IMPLEMENTED** (Phase A + B, built and verified live), **PLANNED** (Phase C, forward), **EVIDENCE-GATED** (Phase D, build only on a measured trigger), and **EXPLICITLY OUT OF SCOPE** (the over-engineering rejections) — each line carries a status and its grounding.

This is the flat, status-marked companion to the narrative roadmap. For *why* the priorities were chosen see [`00-enhancement-overview-and-priorities.md`](./00-enhancement-overview-and-priorities.md); for the full adopt/adapt/defer/skip reasoning see [`02-research-verdict-and-rejections.md`](./02-research-verdict-and-rejections.md). Phase **A** and **B** are **done** — described here as built. Phase **C** and **D** are **forward plans**.

---

## Status legend

| Marker | Meaning |
|--------|---------|
| ✅ **IMPLEMENTED** | Built and **verified live** against real Postgres, over HTTP, and through MCP (not unit mocks). Phase A & B. |
| 📋 **PLANNED** | Documented forward plan, gated on **product signal**. Phase C. Lands behind an existing seam. |
| 🔒 **EVIDENCE-GATED** | Documented forward plan, built **ONLY on a measured trigger**. Phase D. Higher cost; never built speculatively. |
| ⛔ **OUT OF SCOPE** | Over-engineering, **explicitly rejected**. Recorded so it is not re-litigated. |

Every IMPLEMENTED / PLANNED / EVIDENCE-GATED line honours the same hard invariants: no new DB, no new service, no graph-engine swap, no contract break, the LLM never self-edits the shared graph, nothing pushed into SharedCore, and only additive `/v1` + MCP surface.

---

## Feature count at a glance

| Bucket | Status | Count | Phase |
|--------|--------|-------|-------|
| IMPLEMENTED | ✅ | 7 features | A + B |
| PLANNED | 📋 | 3 features | C |
| EVIDENCE-GATED | 🔒 | 3 features | D |
| OUT OF SCOPE | ⛔ | 7 rejections | — |

---

## ✅ IMPLEMENTED — Phase A (confidence-weighted, bitemporally-auditable retrieval)

Built and verified live. Migration `db/migrations/20260614_0003__phaseA.sql` (additive: one nullable column + one partial index; no enum/RLS change). Touches `core/config.py`, `extraction/extractor.py`, `db/graph_repo.py`, `retrieval/orchestrator.py`.

### A-1 · Explicit supersede chain · ✅ IMPLEMENTED

| Field | Value |
|-------|-------|
| **What** | Additive column `edges.supersedes_edge_id`. On a content change, `graph_repo.upsert_extracted_edge` closes the prior **current** edge **and** links the new edge to the one it replaces — an auditable contradiction chain, not an unlinked `valid_to` close. |
| **File / symbol** | `db/graph_repo.py::upsert_extracted_edge` |
| **Config** | — |
| **Research basis** | Zep / Graphiti bi-temporal invalidation (Rasmussen 2025). |
| **Verified** | new→old link present **and** old edge closed. |

### A-2 · Extraction confidence floor · ✅ IMPLEMENTED

| Field | Value |
|-------|-------|
| **What** | One floor governs what *enters* the graph. Below-floor edges are dropped or kept-but-flagged (`metadata.flagged`). One knob, no tiering. |
| **File / symbol** | `extraction/extractor.py::_parse_edges` |
| **Config** | `extraction_confidence_floor=0.6`, `confidence_floor_mode=flag\|drop` |
| **Research basis** | MemGPT precedence (2023, adapted) — confidence as a gate, not a storage tier. |
| **Verified** | low-confidence edges filtered per mode. |

### A-3 · Graph-aware confidence/recency rerank · ✅ IMPLEMENTED

| Field | Value |
|-------|-------|
| **What** | `find_entities` / `keyword_search` surface each entity's **strongest CURRENT edge confidence** (correlated subquery) + `created_at`; the RRF rerank multiplies the fused score by confidence and recency. Folded into the existing RRF — **not** a memory-tier hierarchy. |
| **File / symbol** | `db/graph_repo.py::find_entities`, `::keyword_search`; `retrieval/orchestrator.py::rerank_multiplier` |
| **Config** | `rerank_confidence_weight=1.0`, `rerank_recency_weight=0.5`, `rerank_recency_halflife_days=90` |
| **Research basis** | MemGPT precedence (2023, **adapted** — precedence, not tiering). |
| **Verified** | reranked ordering reflects confidence + recency. |

**Rerank formula (as implemented):**

```
rerank_multiplier = (1 + w_conf * confidence)
                  * ((1 - w_rec) + w_rec * recency_decay)
recency_decay     = 0.5 ** (age_days / halflife_days)
```

No new index type, no LLM judge.

---

## ✅ IMPLEMENTED — Phase B (active memory + change/activity surface)

Built and verified live. Migration `db/migrations/20260614_0004__phaseB.sql` (additive: widens two CHECK enumerations and adds an activity index — existing rows stay valid). Touches `models/canonical.py`, `connectors/github.py`, `db/graph_repo.py`, `copilot/queries.py`, `api/graph.py`, `api/connectors.py`, `extraction/consolidator.py`, `worker/runner.py`, and the `mcp-eng-memory` manifest + invoke path.

### B-1 · Schema widening (enums + activity index) · ✅ IMPLEMENTED

| Field | Value |
|-------|-------|
| **What** | `entities.kind` CHECK widened: `+change`, `+capability`, `+expertise_summary`. `edges.rel` CHECK widened: `+touched`, `+summarizes`. New activity index for time-ordered scans. |
| **File / symbol** | `db/migrations/20260614_0004__phaseB.sql`, `models/canonical.py` |
| **Config** | — |
| **Research basis** | Enabling substrate for B-2 / B-3. |
| **Verified** | additive migration applies; existing rows remain valid. |

### B-2 · Activity / change surface + timeline · ✅ IMPLEMENTED

| Field | Value |
|-------|-------|
| **What** | `connectors/github.py` emits commit-level `change` nodes with `authored` + `touched` edges. `graph_repo.activity_timeline(scope_entity_id, since, until)` returns **current** `change`/`pr`/`ticket`/`incident` nodes connected to a repo or person, time-ordered, with author. |
| **File / symbol** | `connectors/github.py`; `db/graph_repo.py::activity_timeline`; `copilot/queries.activity` |
| **HTTP surface** | `POST /v1/graph/activity` |
| **MCP surface** | `what_changed` tool on `mcp-eng-memory` |
| **Config** | `connector_change_granularity=auto\|commit\|pr_ticket` (configurable, not hard-coded) |
| **Research basis** | MSR issue↔commit linking. |
| **Verified** | cited, time-ordered "who did what when" over real data. |

### B-3 · Reflection / consolidation pass · ✅ IMPLEMENTED

| Field | Value |
|-------|-------|
| **What** | `extraction/consolidator.run_consolidation` clusters each person's **current** `authored` / `reviewed` / `owns` / `expert_in` edges (`graph_repo.consolidation_clusters`). High-confidence clusters become an `expertise_summary` node + `summarizes` evidence edges (`source='consolidation'`). |
| **File / symbol** | `extraction/consolidator.py::run_consolidation`; `db/graph_repo.py::consolidation_clusters`; `worker/runner.py` |
| **Cluster admission** | `avg_confidence >= consolidation_avg_confidence` (**0.75**) **and** `count >= consolidation_min_cluster` (**3**) |
| **Idempotency & cost** | Idempotent + cost-metered via `extraction_jobs`; **supersede-on-rerun** |
| **Boundary** | **GRAPH-ONLY** — never embedded into RAG, never written to Memory |
| **Keyless** | Deterministic keyless fallback (no provider required) |
| **Triggers (both)** | On-demand `POST /v1/extract?consolidate=true` **and** a scheduled worker tick (`worker/runner.py`, `consolidation_schedule_enabled`, enumerating tenants from the **non-RLS outbox**) |
| **Research basis** | Generative Agents reflection (Park 2023). |
| **Verified** | a run produced **2** summaries; an idempotent re-run produced **0**. |

> Phase B is the temporal "who did what when" capability (B-2) plus consolidated expertise (B-3). The LLM never self-edits the shared graph — consolidation writes deterministic `source='consolidation'` edges.

---

## 📋 PLANNED — Phase C (expertise scoring + query-aware fusion + identity resolution)

**Documented, not yet implemented.** Forward plan gated on **product signal** rather than a hard metric trigger. Each item lands behind an existing seam — no new structures.

| # | Feature | Status | What it adds | Where it lands | Research basis |
|---|---------|--------|--------------|----------------|----------------|
| C-1 | Recency-decayed Degree-of-Knowledge `expert_in` + ownership-concentration metric | 📋 PLANNED | Quantifies *how concentrated* ownership is, decayed by recency — a defensible "who is the expert here" derivation rather than raw commit counts. | On the existing `owns` / `expert_in` edges + the Phase-A recency-decay primitive. | Bird 2011 / Fritz 2010 (Degree-of-Knowledge) / Caulo 2020 |
| C-2 | Lightweight regex query-type classifier → per-leg RRF weights | 📋 PLANNED | A deterministic, debuggable classifier picks per-leg fusion weights (graph vs dense vs keyword) by question shape. **No LLM judge, no RRF parameter sweeps.** | Per-leg weights in `retrieval/orchestrator.py` (keeps `retrieval_rrf_k=60` fixed). | Retrieval routing (regex, not learned) |
| C-3 | Semantic identity resolution + human-review queue | 📋 PLANNED | A semantic resolver layered **on top of** the existing exact handle/email match; surfaces candidate merges to a **human-review queue** — never auto-merges. | Resolver atop the exact-match identity path + review queue. | Semantic identity resolution (DEFER → Phase C) |

---

## 🔒 EVIDENCE-GATED — Phase D (advanced retrieval, build ONLY on a measured trigger)

**Documented, deferred.** Each item is sound but its cost is justified only by a **measured** trigger — it is never built speculatively. Each already has a seam (the edge model, the recursive-CTE neighborhood, the consolidator), so deferring costs nothing structurally.

| # | Feature | Status | What it adds | Build trigger | Research basis |
|---|---------|--------|--------------|---------------|----------------|
| D-1 | SZZ change→cause attribution into `caused` edges | 🔒 EVIDENCE-GATED | An SZZ-style heuristic populating `caused` edges (change→introducing-cause). | Incident/defect attribution becomes a **requested** query with ground truth to validate against. | SZZ defect attribution |
| D-2 | Single-hop Personalized-PageRank reviewer recommendation | 🔒 EVIDENCE-GATED | A **single-hop** PPR reviewer-recommendation only — never full multi-hop. | The recursive-CTE neighborhood proves insufficient for a real reviewer-routing query. | HippoRAG PPR (single-hop variant only) |
| D-3 | RAPTOR L2 meta-consolidation | 🔒 EVIDENCE-GATED | A second consolidation tier (summaries-of-summaries) over Phase-B `expertise_summary` nodes. | A tenant exceeds **>1k edges** **and** single-level consolidation demonstrably under-summarizes. | RAPTOR (Sarthi 2024) |

> Also deferred (no Phase assigned until a trigger appears): **A-MEM active reinforcement** (build on measured drift between confidence and observed corroboration frequency) and a **cross-encoder / learned reranker** (build only when RRF-only ordering is measured as the dominant quality ceiling).

---

## ⛔ OUT OF SCOPE — explicitly rejected as over-engineering

Recorded prominently so they are **not re-proposed**. Each fails the over-engineering screen for a single-Postgres, multi-tenant, contract-first app whose design is already at 2024–2025 SOTA. These are decisions, not omissions.

| # | Rejected technique | Status | Why rejected |
|---|--------------------|--------|--------------|
| X-1 | **Mem0** (hosted/vendor agent memory) | ⛔ OUT OF SCOPE | We **own** the graph; it must never leave the app or enter SharedCore. Vendoring it surrenders RLS/contract control. |
| X-2 | **Microsoft GraphRAG** — community detection / **Leiden** | ⛔ OUT OF SCOPE | Needs a graph engine we deliberately don't have; the summarization win is already covered by Phase-B consolidation without clustering the whole graph. |
| X-3 | **Code Property Graphs** (AST + CFG + PDG) | ⛔ OUT OF SCOPE | We model *people, changes, ownership, expertise over time* — not program semantics. A CPG is a different product. |
| X-4 | **Neo4j / Apache AGE migration** | ⛔ OUT OF SCOPE | No engine swap. Frozen `pgvector/pgvector:pg16`; `cxa1_user` cannot `CREATE EXTENSION`. The `GraphRetriever` seam exists so a later swap is possible — but nothing measured justifies it. |
| X-5 | **RankGPT / LLM-judge reranking** | ⛔ OUT OF SCOPE | Non-deterministic and costly; the **LLM never judges/self-edits the shared graph** is a hard guardrail. Confidence+recency RRF rerank (A-3) is deterministic and auditable. |
| X-6 | **RRF parameter sweeps** | ⛔ OUT OF SCOPE | Adds tuning surface and reproducibility risk for marginal gain. `retrieval_rrf_k=60` stays fixed; the regex query-type classifier (C-2) is the bounded, deterministic alternative. |
| X-7 | **Interaction-network centrality** | ⛔ OUT OF SCOPE | Optimizes "who is central in the social graph," not "who owns/changed this code." Bird/Fritz recency-decayed ownership (C-1) answers the real expertise question. |

---

## Over-engineering guardrails (binding on every feature, present and future)

These constrain Phase C and D as much as they constrained A and B. Any new proposal must clear all of them before it is even prototyped:

- **No new DB and no new service.**
- **No graph-engine swap** (the frozen `pgvector/pgvector:pg16` adjacency-list + recursive-CTE graph stays; `cxa1_user` cannot `CREATE EXTENSION`).
- **No community detection / Leiden, no Code Property Graphs.**
- **No PPR-multihop and no RAPTOR-L2 before a measured need** (Phase D is trigger-gated).
- **No LLM-judge reranking.**
- **Exactly one confidence floor** (`extraction_confidence_floor`) and **exactly one consolidation threshold pair** (`consolidation_avg_confidence=0.75`, `consolidation_min_cluster=3`) — no proliferating knobs.
- **The LLM never self-edits the shared graph.**
- **Nothing is pushed into SharedCore** — it stays generic, no business logic.
- **Only additive `/v1` + MCP** — no contract is broken.

---

*See also: [`00-enhancement-overview-and-priorities.md`](./00-enhancement-overview-and-priorities.md), [`02-research-verdict-and-rejections.md`](./02-research-verdict-and-rejections.md), [`phase-A-confidence-weighted-bitemporal-retrieval.md`](./phase-A-confidence-weighted-bitemporal-retrieval.md), [`phase-B-reflection-and-activity-surface.md`](./phase-B-reflection-and-activity-surface.md), and the MVP design set in [`../docs/`](../docs/).*
