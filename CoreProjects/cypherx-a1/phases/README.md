# phases/ index

> The cypherx-a1 (Autonomous Engineering Memory) **enhancement roadmap**: four research-driven, strictly-additive phases that sharpen the temporal-graph copilot — **A & B are built + verified live; C & D are forward plans** gated on measured need.

This index sits alongside the design set in [`../docs/`](../docs/) (00–17 + ADRs), which describes the **MVP product** (graph + RAG + Memory + hybrid RRF retrieval + cited copilot + the stateless `mcp-eng-memory` server). The **`phases/` set documents what comes after the MVP** — a sequence of surgical upgrades to retrieval quality and the activity/expertise surface, each one honouring the same hard invariants (no new DB, no new service, no graph-engine swap, no contract break, the graph never enters SharedCore).

---

## Priority

The product goal is to **make a real difference in development** — every repo + Jira/etc. registers and tracks *"what changed and who worked on what, over time,"* queryable by humans and AI agents. Against that goal the highest-leverage surfaces are the two that the copilot most often answers on:

| Priority | Surface | What it powers |
|----------|---------|----------------|
| **1** | **Knowledge base / graph quality** | `who built what`, `who owns this`, `what breaks if I change X`, `why was this decided` — confidence-weighted, bitemporally-auditable, recency-aware. |
| **2** | **Memory / expertise** | `who knows about X`, `who did what when` — consolidated expertise summaries + a time-ordered activity surface. |

Everything in this roadmap serves KB-and-memory quality first; it adds **no new capability category** the MVP lacks.

---

## Research-driven approach

Each phase is grounded in a **5-agent research pass over 38 works** in temporal-graph agent memory, code ownership/expertise, and hybrid retrieval. The pass produced one verdict and three buckets.

### Key finding — already SOTA; wins are surgical

> **The MVP design is already at 2024–2025 state-of-the-art for temporal-graph agent memory.** There is no missing foundational piece to bolt on. Every worthwhile improvement is **surgical**: it reuses the existing `cypherx_a1` Postgres graph, the existing RRF orchestrator, and the existing `/v1` + MCP surfaces. **No new database, no new service, no engine swap, no contract change** is justified by the evidence — so the roadmap deliberately spends its budget on a handful of high-confidence, additive refinements rather than a re-platforming.

### Buckets

| Bucket | Meaning | Where it lands |
|--------|---------|----------------|
| **ADOPT** | High-confidence, evidence-backed, low-risk → build now. | Phases **A**, **B**, **C** |
| **DEFER** | Plausible but evidence-gated → build **only on a measured trigger**. | Phases **C**, **D** |
| **SKIP** | Over-engineering, explicitly rejected. | (never) |

**ADOPT lineage** (source → where it lands):

| Source work | Adoption | Phase |
|-------------|----------|-------|
| Generative Agents reflection (Park 2023) | Consolidation pass → `expertise_summary`/`capability` nodes | **B** |
| Zep/Graphiti bi-temporal (Rasmussen 2025) | Explicit `supersedes_edge_id` + conflict precedence | **A** |
| MemGPT precedence (2023, *adapted*) | Edge confidence + recency into RRF rerank — **not** the tiering | **A** |
| Bird 2011 ownership / Fritz 2010 Degree-of-Knowledge / Caul 2020 | Recency-decayed `expert_in` + ownership concentration | **C** |
| MSR issue–commit linking | Activity/change surface + timeline | **B** |

**DEFER** (build only on a measured trigger): RAPTOR L2 meta-consolidation, A-MEM active reinforcement, HippoRAG Personalized-PageRank multi-hop, SZZ defect attribution, semantic identity resolution, cross-encoder reranker.

**SKIP** (explicitly rejected as over-engineering): Mem0 (vendor lock-in), Microsoft GraphRAG community-detection / Leiden, Code Property Graphs (AST+CFG+PDG), Neo4j/AGE migration, RankGPT LLM-judge reranking, RRF parameter sweeps, interaction-network centrality.

---

## The four phases

| Phase | Theme | Status | Doc |
|-------|-------|--------|-----|
| **A** | Confidence-weighted, bitemporally-auditable retrieval | ✅ **Built + verified live** | [phase-a.md](./phase-a.md) |
| **B** | Active memory (consolidation) + the change/activity surface | ✅ **Built + verified live** | [phase-b.md](./phase-b.md) |
| **C** | Expertise scoring + query-aware fusion + identity resolution | 📋 Planned | [phase-c.md](./phase-c.md) |
| **D** | Evidence-gated advanced retrieval | 📋 Planned (trigger-gated) | [phase-d.md](./phase-d.md) |

> Legend: ✅ implemented + verified · 📋 documented forward plan.

### Phase A — confidence-weighted, bitemporally-auditable retrieval ✅

Built and verified against real Postgres, over HTTP, and through MCP. Migration `db/migrations/20260614_0003__phaseA.sql` is additive (one nullable column + one partial index; no enum/RLS change).

- **Explicit supersede chain** — `edges.supersedes_edge_id`. `graph_repo.upsert_extracted_edge` closes the prior *current* edge on a content change and links the new edge to the one it replaced, so a contradiction (ownership reassigned, a relation revised when its source artifact changes) is an **auditable chain**, not an unlinked `valid_to` close. *Verified: new→old link present + old edge closed.* Grounded in Zep/Graphiti bi-temporal invalidation.
- **Extraction confidence floor** — `extractor._parse_edges` applies `extraction_confidence_floor=0.6` under `confidence_floor_mode` (`flag` | `drop`): below-floor edges are dropped, or kept-but-flagged (`metadata.flagged`). **One** floor, one knob.
- **Graph-aware rerank** — `graph_repo.find_entities` / `keyword_search` surface each entity's **strongest current-edge confidence** (correlated subquery) + `created_at`; `retrieval/orchestrator.rerank_multiplier` = `(1 + w_conf·confidence) · ((1−w_rec) + w_rec·recency_decay)`, with `recency = 0.5 ** (age_days / halflife)`. Config: `rerank_confidence_weight=1.0`, `rerank_recency_weight=0.5`, `rerank_recency_halflife_days=90`. This is the MemGPT-precedence idea **adapted into the RRF rerank**, not a memory-tier hierarchy.

### Phase B — active memory + change/activity surface ✅

Built and verified live. Migration `db/migrations/20260614_0004__phaseB.sql` widens two CHECK enumerations and adds an activity index (all additive — existing rows stay valid).

- **Widened enums** — `entities.kind` += `change`, `capability`, `expertise_summary`; `edges.rel` += `touched`, `summarizes`; plus `idx_entities_activity`.
- **Activity / change surface** — `connectors/github.py` emits commit-level `change` nodes (`authored` + `touched` edges) at `connector_change_granularity=auto|commit|pr_ticket`. `graph_repo.activity_timeline(scope_entity_id, since, until)` returns current `change`/`pr`/`ticket`/`incident` nodes connected to a repo or person, time-ordered, with author. Exposed as `POST /v1/graph/activity` (`copilot/queries.activity`) and the MCP **`what_changed`** tool. *Verified live: cited, time-ordered "who did what when".*
- **Reflection / consolidation pass** — `extraction/consolidator.run_consolidation` clusters each person's current `authored`/`reviewed`/`owns`/`expert_in` edges (`graph_repo.consolidation_clusters`); high-confidence clusters (avg ≥ `consolidation_avg_confidence=0.75`, count ≥ `consolidation_min_cluster=3`) become an **`expertise_summary` node + `summarizes` evidence edges** (`source='consolidation'`). Idempotent + cost-metered via `extraction_jobs`, supersede-on-rerun, **graph-only (never embedded into RAG)**, with a keyless deterministic fallback. Triggers (both): `POST /v1/extract?consolidate=true` and a scheduled worker tick (`worker/runner.py`, `consolidation_schedule_enabled`, enumerating tenants from the non-RLS `outbox`). This is the Generative-Agents reflection idea. *Verified live: 2 summaries; idempotent re-run → 0.* **One** consolidation threshold; the LLM never self-edits the shared graph.

### Phase C — expertise scoring + query-aware fusion + identity resolution 📋

**Documented, not yet implemented.** Forward plan:

- **Recency-decayed Degree-of-Knowledge `expert_in`** + an **ownership-concentration** metric (Bird 2011 / Fritz 2010 / Caul 2020).
- A **lightweight regex query-type classifier** → per-leg RRF weights (graph vs dense vs keyword tuned per question shape).
- **Semantic identity resolution** + a **human-review queue**, layered *on top of* the existing exact handle/email match (never replacing it).

### Phase D — evidence-gated advanced retrieval 📋

**Documented, build ONLY on a measured trigger** — each item ships only when a metric demonstrates the need:

- **SZZ** change→cause attribution into `caused` edges.
- **Single-hop Personalized-PageRank** reviewer recommendation.
- **RAPTOR L2** meta-consolidation for >1k-edge tenants.

---

## Over-engineering guardrails (apply to every phase)

These are load-bearing and constrain C and D as much as they constrained A and B:

- **No new DB and no new service.** No graph-engine swap (the frozen `pgvector/pg16` adjacency-list + recursive-CTE graph stays; `cxa1_user` cannot `CREATE EXTENSION`).
- **No community detection / Leiden, no Code Property Graphs.**
- **No PPR-multihop and no RAPTOR-L2 before a measured need** (Phase D is trigger-gated).
- **No LLM-judge reranking.**
- **Exactly one confidence floor and exactly one consolidation threshold** — resist proliferation of knobs.
- **The LLM never self-edits the shared graph.**
- **Nothing is pushed into SharedCore** (it stays generic — no business logic).
- **Only additive `/v1` + MCP** — no contract is broken.

---

*See also: [`../docs/14-build-plan-and-phasing.md`](../docs/14-build-plan-and-phasing.md) (MVP phases 0–3) and [`../docs/17-open-questions-and-roadmap.md`](../docs/17-open-questions-and-roadmap.md).*
