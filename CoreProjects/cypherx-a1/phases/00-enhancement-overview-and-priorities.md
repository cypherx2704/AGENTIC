# Enhancement overview & priorities

> The cypherx-a1 **enhancement roadmap**: a research-driven, surgical plan to make the Autonomous Engineering Memory measurably better at its one job — answering *"what changed and who worked on what, over time"* for humans and AI agents — with **Phase A & B built and verified live**, and **Phase C & D documented as forward, evidence-gated plans**. No new DB, no new service, no engine swap, no contract break.

---

## Why this roadmap exists

cypherx-a1 is **Autonomous Engineering Memory**: a bitemporal Postgres adjacency-list knowledge graph (entities + typed edges; **no Apache AGE** — the runtime sits on a frozen `pgvector/pgvector:pg16` image and the `cxa1_user` role cannot `CREATE EXTENSION`) layered with SharedCore RAG vectors, SharedCore Memory, hybrid RRF retrieval, a cited copilot, and the stateless `mcp-eng-memory` MCP server. Multi-tenant RLS throughout. SharedCore stays generic — **no business logic is pushed into it**.

The **product goal** is concrete and it is the lens every enhancement is judged against:

> Make a real difference in day-to-day development. Every repo + Jira/issue source **registers and tracks "what changed and who worked on what, over time"**, queryable by humans *and* by AI coding agents.

Two capabilities serve that goal directly and are therefore the **priority**:

1. **Knowledge Base (the graph)** — the durable, auditable record of entities, ownership, and typed relationships.
2. **Memory of activity over time** — the change/activity surface and the temporal "who did what when" timeline.

This roadmap exists to push *those two* forward without re-architecting anything. The graph is the crown jewel; it is app-owned, never embedded into RAG, never written into per-principal Memory. Everything below respects that boundary.

---

## The research-driven method

Before writing a line of enhancement code, a **5-agent research pass surveyed 38 works** across temporal-graph agent memory, code-ownership / expertise mining, and hybrid retrieval. Every candidate technique was scored on two axes:

- **Fit** — does it advance the product goal (track change + ownership over time, queryable)?
- **Over-engineering risk** — does it add a DB, a service, an engine swap, a contract change, or unbounded complexity for marginal gain?

The headline conclusion shaped the entire roadmap:

> The existing design is **already at 2024–2025 SOTA** for temporal-graph agent memory. Therefore the wins are **surgical** — additive `/v1` + MCP only, one config knob per behavior, no structural change.

Each surveyed technique was sorted into exactly one bucket: **ADOPT** (build now, Phase A/B), **DEFER** (evidence-gated, build only on a measured trigger, Phase C/D), or **SKIP** (over-engineering, explicitly rejected).

### ADOPT — what made it in, and where it landed

| Research basis | Technique adopted | Lands in |
| --- | --- | --- |
| Generative Agents reflection (Park 2023) | Periodic consolidation/reflection pass → `expertise_summary` / `capability` nodes | **Phase B** |
| Zep / Graphiti bi-temporal (Rasmussen 2025) | Explicit `supersedes_edge_id` + conflict precedence (auditable contradiction chain) | **Phase A** |
| MemGPT precedence (2023, **ADAPT**) | Edge **confidence + recency** folded into RRF rerank — **not** the memory-tiering | **Phase A** |
| Bird 2011 ownership / Fritz 2010 Degree-of-Knowledge / Caul 2020 | Recency-decayed `expert_in` + ownership-concentration metric | **Phase C** |
| MSR issue–commit linking | Activity/change surface + temporal timeline | **Phase B** |

### DEFER — evidence-gated, build only on a measured trigger

These are sound but unproven *for this product at current scale*. They are documented so the trigger is explicit, and they are **not** built speculatively:

- RAPTOR L2 meta-consolidation (build for >1k-edge tenants).
- A-MEM active reinforcement.
- HippoRAG Personalized-PageRank multi-hop (Phase D considers a *single-hop* variant only).
- SZZ defect attribution.
- Semantic identity resolution (beyond exact handle/email match).
- Cross-encoder reranker.

### SKIP — over-engineering, explicitly rejected

Recorded so they are not re-proposed: **Mem0** (vendor lock-in), **Microsoft GraphRAG** community-detection / Leiden, **Code Property Graphs** (AST+CFG+PDG), **Neo4j / Apache AGE migration**, **RankGPT** LLM-judge reranking, **RRF parameter sweeps**, **interaction-network centrality**.

---

## Headline findings

1. **The design is already SOTA.** The bitemporal adjacency-list graph + hybrid RRF retrieval + cited copilot is competitive with 2024–2025 temporal-graph memory systems. The job is to *sharpen*, not rebuild.
2. **The biggest near-term wins are temporal + ownership**, exactly the product goal: an auditable contradiction chain (bi-temporal supersede), a queryable activity timeline, and consolidated expertise summaries.
3. **Reranking beats re-engineering.** Folding edge confidence + recency into the existing RRF rerank (the MemGPT *precedence* idea, adapted) is high-impact and low-risk — no new index type, no LLM judge.
4. **Reflection belongs in the graph, never in RAG/Memory.** Consolidation produces graph-only `expertise_summary` nodes; it is never embedded into the RAG corpus and never written to per-principal Memory.
5. **Most "advanced" graph techniques are traps at this scale.** Community detection, PPR-multihop, CPGs, and engine migrations all fail the over-engineering screen until a measured need appears.

---

## Phase map

Two phases are **built and verified**; two are **forward plans**. Impact/effort are relative to the product goal.

| Phase | Theme | Status | Impact | Effort | Gated by |
| --- | --- | --- | --- | --- | --- |
| **A** | Bi-temporal supersede + confidence/recency rerank | ✅ Implemented + verified live | High | Low | — |
| **B** | Activity/change surface + reflection/consolidation | ✅ Implemented + verified live | High | Medium | — |
| **C** | Ownership/expertise metrics + query-type RRF weights + semantic identity (review-gated) | 📋 Planned | Medium | Medium | Product signal |
| **D** | SZZ attribution + single-hop PPR reviewer rec + RAPTOR L2 | 📋 Planned (evidence-gated) | Medium | High | **Measured trigger only** |

> Verification scope for A & B: exercised against **real Postgres**, over **HTTP**, and through the **MCP** server — not unit mocks.

---

## Phase A — built & verified

Bi-temporal precedence and a confidence/recency-aware rerank. Migration `db/migrations/20260614_0003__phaseA.sql`; touches `core/config.py`, `extraction/extractor.py`, `db/graph_repo.py`, `retrieval/orchestrator.py`.

| Capability | What was built | File / symbol | Config keys | Verified |
| --- | --- | --- | --- | --- |
| **Explicit supersede chain** | `edges.supersedes_edge_id` (additive column). `graph_repo.upsert_extracted_edge` closes the prior *current* edge on a content change and links the new edge to it — an auditable contradiction chain. | `db/graph_repo.py::upsert_extracted_edge` | — | new→old link present **and** old edge closed |
| **Extraction confidence floor** | Below-floor edges are dropped or kept-but-flagged (`metadata.flagged`), one knob, no tiering. | `extraction/extractor.py::_parse_edges` | `extraction_confidence_floor=0.6`, `confidence_floor_mode=flag\|drop` | low-confidence edges filtered per mode |
| **Graph-aware rerank** | `find_entities` / `keyword_search` surface each entity's **strongest CURRENT edge confidence** (correlated subquery) + `created_at`; rerank multiplies the fused score by confidence and recency. | `db/graph_repo.py::find_entities`, `::keyword_search`; `retrieval/orchestrator.py::rerank_multiplier` | `rerank_confidence_weight=1.0`, `rerank_recency_weight=0.5`, `rerank_recency_halflife_days=90` | reranked ordering reflects confidence + recency |

**Rerank formula (as implemented):**

```
rerank_multiplier = (1 + w_conf * confidence)
                  * ((1 - w_rec) + w_rec * recency_decay)
recency_decay     = 0.5 ** (age_days / halflife_days)
```

This is the MemGPT *precedence* idea **adapted into the existing RRF rerank** — deliberately *not* MemGPT's memory-tiering. No new index, no LLM judge.

---

## Phase B — built & verified

The activity/change surface and the reflection/consolidation pass — the temporal "who did what when" capability plus consolidated expertise. Migration `db/migrations/20260614_0004__phaseB.sql`; touches `models/canonical.py`, `connectors/github.py`, `db/graph_repo.py`, `copilot/queries.py`, `api/graph.py`, `api/connectors.py`, `extraction/consolidator.py`, `worker/runner.py`, and the `mcp-eng-memory` manifest + invoke path.

### Schema widening

- `kind` CHECK widened: `+change`, `+capability`, `+expertise_summary`.
- `rel` CHECK widened: `+touched`, `+summarizes`.
- New **activity index** for time-ordered scans.

### Activity / change surface

| Element | What it does | File / symbol |
| --- | --- | --- |
| Commit-level `change` nodes | `connectors/github.py` emits a `change` node per commit, with `authored` + `touched` edges. | `connectors/github.py` |
| Granularity knob | `connector_change_granularity=auto\|commit\|pr_ticket` chooses commit-level vs PR/ticket-level change nodes. | `core/config.py` |
| Timeline read | `activity_timeline(scope_entity_id, since, until)` returns **current** change/pr/ticket/incident nodes connected to a repo/person, time-ordered, with author. | `db/graph_repo.py::activity_timeline` |
| HTTP surface | `POST /v1/graph/activity` (handler in `copilot/queries.activity`). | `api/graph.py` |
| MCP surface | `what_changed` tool on `mcp-eng-memory`. | `mcp-eng-memory` manifest + invoke |

**Verified live:** a cited, time-ordered *"who did what when"* result over real data.

### Reflection / consolidation pass

`extraction/consolidator.run_consolidation` clusters each person's **current** `authored` / `reviewed` / `owns` / `expert_in` edges (via `graph_repo.consolidation_clusters`). A high-confidence cluster becomes an `expertise_summary` node plus `summarizes` **evidence edges**.

| Property | Behavior |
| --- | --- |
| Cluster admission | `avg_confidence >= consolidation_avg_confidence` (**0.75**) **and** `count >= consolidation_min_cluster` (**3**). |
| Output | One `expertise_summary` node + `summarizes` evidence edges, `source='consolidation'`. |
| Idempotency & cost | Idempotent + cost-metered via `extraction_jobs`; **supersede-on-rerun**. |
| Boundary | **GRAPH-ONLY** — never embedded into RAG, never written to Memory. |
| Keyless | Deterministic keyless fallback (no provider required). |

**Triggers (both):**

- On-demand: `POST /v1/extract?consolidate=true`.
- Scheduled: a worker tick (`worker/runner.py`, `consolidation_schedule_enabled`) that enumerates tenants from the **non-RLS outbox**.

**Verified live:** a run produced **2 summaries**; an idempotent re-run produced **0** new summaries.

---

## Phase C — planned

Forward plan; **not yet implemented.** Sharpens ownership/expertise signals and routing, gated by product signal rather than a hard metric trigger.

| Item | Research basis | Notes |
| --- | --- | --- |
| Recency-decayed **Degree-of-Knowledge** `expert_in` + **ownership-concentration** metric | Bird 2011 / Fritz 2010 / Caul 2020 | Quantifies *how concentrated* ownership is, decayed by recency. |
| Lightweight **regex query-type classifier** → per-leg RRF weights | (retrieval routing) | Cheap classifier picks per-leg weights; **no LLM judge**, no RRF parameter sweeps. |
| **Semantic identity resolution** + human-review queue | (identity) | Layered **on top of** exact handle/email match; never auto-merges without review. |

---

## Phase D — planned, evidence-gated

Forward plan; **build ONLY on a measured trigger.** These are the higher-cost techniques the research pass deferred; each carries an explicit gate so it is never built speculatively.

| Item | Research basis | Build trigger |
| --- | --- | --- |
| **SZZ** change→cause attribution into `caused` edges | SZZ defect attribution | A measured need for defect-cause links. |
| **Single-hop Personalized-PageRank** reviewer recommendation | HippoRAG PPR (single-hop variant only) | A measured reviewer-routing need. |
| **RAPTOR L2** meta-consolidation | RAPTOR | **Tenants with >1k edges** where single-level consolidation underperforms. |

---

## User-locked decisions

These were decided by the user and are binding for this roadmap:

| Decision | Locked value |
| --- | --- |
| **Deliverable scope** | Ship **Phase A & B** (built + verified). C & D remain documented forward plans. |
| **Change-node granularity** | **Configurable** — `connector_change_granularity=auto\|commit\|pr_ticket` (not hard-coded). |
| **Consolidation triggers** | **Both** — on-demand `POST /v1/extract?consolidate=true` **and** the scheduled worker tick (`consolidation_schedule_enabled`). |

---

## Over-engineering guardrails (binding)

Every enhancement, present and future, must hold to these. They are the operationalized output of the over-engineering screen:

- **No new DB and no new service.**
- **No graph-engine swap** (no Neo4j / Apache AGE; adjacency-list + recursive-CTE on frozen `pgvector/pg16` stays).
- **No community detection and no Code Property Graphs.**
- **No PPR-multihop and no RAPTOR-L2 before a measured need.**
- **No LLM-judge reranking.**
- **One** confidence floor and **one** consolidation threshold — no proliferating knobs.
- **The LLM never self-edits the shared graph.**
- **Nothing is pushed into SharedCore** — it stays generic.
- **Only additive `/v1` + MCP** surface — **no contract broken.**

---

## How to read this set

This is doc `00` of the cypherx-a1 enhancement-roadmap (`phases/`). It is the entry point: it states *why* the roadmap exists, *how* the priorities were chosen (5 agents / 38 works / fit + over-engineering scoring), *what* was found, and the A/B/C/D phase map with its user-locked decisions. Phase A & B describe **shipped, live-verified** behavior; Phase C & D are **forward plans** — C on product signal, D on a measured trigger. For the platform-level mental model and the SharedCore boundary this roadmap respects, see [`../CLAUDE.md`](../CLAUDE.md) and the product docs in [`../docs/`](../docs/).
