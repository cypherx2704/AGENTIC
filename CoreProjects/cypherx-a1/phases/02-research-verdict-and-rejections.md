# 02 — Research verdict & rejections

> A 5-agent research pass over 38 works concluded that cypherx-a1's design — a bitemporal Postgres adjacency-list knowledge graph + SharedCore RAG/Memory + hybrid RRF retrieval + cited copilot + a stateless MCP server — is **already at 2024–2025 SOTA** for temporal-graph agent memory. The wins are therefore **surgical**: every adopted idea lands behind an existing seam with **no new DB, no new service, no graph-engine swap, no contract change**. This doc is the full **adopt / adapt / defer / skip** ledger, weighted heavily toward memory and knowledge-base prior art, and it names — prominently — what we **explicitly reject as over-engineering**.

This is the verdict companion to the enhancement roadmap. It does not re-derive the architecture (see `docs/00`–`docs/17`); it records *which external work we read, how each maps onto our code, and why we adopted, adapted, deferred, or rejected it*. Phase **A** and Phase **B** are **IMPLEMENTED and verified live** (against real Postgres, over HTTP, and over MCP) — they are described below as **built**. Phase **C** and Phase **D** are **forward plans** behind named seams.

---

## 1. The baseline being measured against

Everything below is graded against the *as-built* MVP, because the research conclusion is that we are not behind the literature — we are at it. The relevant load-bearing baseline:

| Baseline capability | Where it already lives | Why it pre-empts a whole class of "additions" |
|---|---|---|
| Bitemporal graph (valid-time + system-time, current-edge closing) | `db/graph_repo.py`; migration `db/migrations/20260614_0003__phaseA.sql` | Zep/Graphiti's headline feature is *already the substrate*, not a bolt-on. |
| Adjacency list + recursive CTE (no Apache AGE) | `db/graph_repo.py` (`find_entities`, `keyword_search`); ADR D13 | A graph *engine* swap is unnecessary; frozen `pgvector/pgvector:pg16`, `cxa1_user` cannot `CREATE EXTENSION`. |
| Hybrid RRF retrieval (dense RAG ⊕ keyword tsvector) | `retrieval/orchestrator.py` (`RetrievalOrchestrator.retrieve`, `retrieval_rrf_k=60`) | The fusion layer the literature reaches for is *already the seam* where reranking signals plug in. |
| App-owned graph, never embedded into RAG/Memory | ADR D5; `ingestion/pipeline.py` | Removes the leakage/cost failure mode that motivates several rejected designs. |
| Cited copilot + stateless `mcp-eng-memory` | `copilot/service.py`; `mcp-eng-memory/` (ADR D12) | Citations are first-class, so "add provenance" is not an open task. |

The grading rubric: an idea is **ADOPTED** only if it is additive, lands behind an existing seam, and earns its keep on the product goal (*"what changed and who worked on what, over time"*). It is **ADAPTED** if we take the principle but not the machinery. It is **DEFERRED** if it is sound but only justified by a *measured* trigger we have not yet hit. It is **SKIPPED** if it is over-engineering for a single-Postgres, multi-tenant, contract-first app.

---

## 2. The verdict at a glance

| Verdict | Count | What it means for the build |
|---|---|---|
| **ADOPT / ADAPT** | 6 lines | Built in Phase A/B (some Phase C). Surgical, behind a seam. |
| **DEFER (evidence-gated)** | 6 lines | Sound; build ONLY on a measured trigger (Phase C/D). |
| **SKIP (explicitly rejected)** | 7 lines | Over-engineering for this app. Documented so they are not re-litigated. |

---

## 3. ADOPT / ADAPT — the surgical wins (built)

These are the only externally-motivated changes that landed. Each maps to a concrete file, function, table/column, or config key.

| # | Work (year) | Key idea | Mapping into cypherx-a1 | Verdict & reason |
|---|---|---|---|---|
| A1 | **Generative Agents** — Park et al. 2023 | A periodic **reflection** pass clusters raw observations into higher-level, durable summaries the agent reasons over. | **Phase B consolidation pass.** `extraction/consolidator.run_consolidation` clusters each person's *current* `authored`/`reviewed`/`owns`/`expert_in` edges (`graph_repo.consolidation_clusters`); high-confidence clusters become an **`expertise_summary`** node + `summarizes` evidence edges. | **ADOPT.** Reflection is exactly the "raise signal above raw events" move our expertise queries need. Constrained: **graph-only** (never embedded into RAG), idempotent, cost-metered via `extraction_jobs`, supersede-on-rerun. |
| A2 | **Zep / Graphiti** — Rasmussen et al. 2025 | A **bi-temporal** knowledge graph where edges are explicitly invalidated and superseded, giving auditable contradiction handling. | **Phase A explicit supersession.** Additive column **`edges.supersedes_edge_id`**; `graph_repo.upsert_extracted_edge` closes the prior current edge on a content change and links the new edge to it (auditable contradiction chain) + conflict precedence. | **ADOPT.** Our graph was *already* bitemporal; this makes the *contradiction chain itself* a first-class, queryable link rather than an implicit time gap. Verified: new→old link + old closed. |
| A3 | **MemGPT** — Packer et al. 2023 | OS-style **memory tiering** + recency/precedence so the most relevant, freshest facts win. | **Phase A rerank — ADAPT (precedence, NOT tiering).** We take *precedence by confidence+recency* and fold it into RRF rerank; we reject the paging/tier machinery. `graph_repo.find_entities`/`keyword_search` surface each entity's strongest **current** edge confidence (correlated subquery) + `created_at`; `orchestrator.rerank_multiplier = (1 + w_conf*confidence) * ((1-w_rec) + w_rec*recency_decay)`, `recency = 0.5**(age_days/halflife)`. Config: `rerank_confidence_weight=1.0`, `rerank_recency_weight=0.5`, `rerank_recency_halflife_days=90`. | **ADAPT.** The *signal* (precedence) is valuable; the *mechanism* (a tiered memory OS) is over-engineering on a single Postgres with RRF already in place. |
| A4 | **MSR issue↔commit linking** — Mining-Software-Repositories line of work | Linking issues/tickets to the commits that resolve them yields a defensible **change timeline**. | **Phase B activity/change surface + timeline.** `connectors/github.py` emits commit-level **`change`** nodes (`authored` + `touched` edges); `graph_repo.activity_timeline(scope_entity_id, since, until)` returns current `change`/`pr`/`ticket`/`incident` nodes connected to a repo/person, time-ordered, with author. Exposed via `POST /v1/graph/activity` (`copilot/queries.activity`) and the MCP **`what_changed`** tool. | **ADOPT.** This *is* the product goal made literal: cited, time-ordered "who did what when." Verified live. |
| A5 | **Bird et al. 2011 (ownership)** + **Fritz et al. 2010 (Degree-of-Knowledge)** + **Caulo et al. 2020** | Code ownership / expertise is best modeled as **recency-decayed authorship concentration**, not raw commit counts. | **Phase C plan.** Recency-decayed `expert_in` derivation + an **ownership-concentration** metric, layered on the existing `owns`/`expert_in` edges and the Phase-A recency-decay primitive. | **ADOPT (Phase C, forward plan).** The exact theory behind a *good* "who is the expert here" answer. Lands behind the existing edge model — no new structures. |
| A6 | **MemGPT precedence ⇒ RRF** (same family as A3) | Confidence and recency as *ranking* signals rather than *storage* policy. | Already realized in `retrieval/orchestrator.py` rerank (see A3). Single confidence floor governs what *enters* the graph: `extractor._parse_edges` with `extraction_confidence_floor=0.6` and `confidence_floor_mode` (`flag`\|`drop`). | **ADAPT.** One floor in, one precedence rank out. No parameter zoo. |

> **Phase A/B are done.** A2/A3/A6 = Phase A (`...0003__phaseA.sql`); A1/A4 = Phase B (`...0004__phaseB.sql`, widened `kind` CHECK `+change/+capability/+expertise_summary`, `rel` CHECK `+touched/+summarizes`, activity index). A5 is the only ADOPT still forward (Phase C).

### 3.1 Why these were safe to adopt

Every one of A1–A6 satisfies the guardrails simultaneously: additive `/v1` + MCP only (no contract broken), no new DB/service, no graph-engine swap, the **LLM never self-edits the shared graph** (consolidation writes deterministic `summarizes` edges with `source='consolidation'`; it has a keyless deterministic fallback), nothing pushed into SharedCore, and exactly **one confidence floor** (`extraction_confidence_floor`) + exactly **one consolidation threshold pair** (`consolidation_avg_confidence=0.75`, `consolidation_min_cluster=3`).

---

## 4. DEFER — sound, but evidence-gated

These are good ideas whose cost is only justified by a **measured** trigger. They are designed-for behind a seam but **not built**; building one early would be the over-engineering we are avoiding. Phase **C** = low-risk additive; Phase **D** = only on a measured need.

| # | Work | Key idea | Mapping / where it would land | Trigger to build | Phase |
|---|---|---|---|---|---|
| D1 | **RAPTOR** — Sarthi et al. 2024 (L2 meta-consolidation) | Recursive tree of summaries-of-summaries for very large corpora. | A second consolidation tier over Phase-B `expertise_summary` nodes (summaries-of-summaries). | A tenant exceeds **>1k edges** *and* single-level consolidation demonstrably under-summarizes. | D |
| D2 | **A-MEM** — active memory reinforcement | Memories actively strengthen/reinforce on re-access. | A reinforcement signal on edge confidence on repeated corroboration. | Measured drift between confidence and observed corroboration frequency. | D |
| D3 | **HippoRAG** — Personalized-PageRank multi-hop | PPR over the graph for multi-hop associative retrieval. | A *single-hop* PPR **reviewer-recommendation** only (never full multi-hop). | Recursive-CTE neighborhood proves insufficient for a real reviewer-rec query. | D |
| D4 | **SZZ** — defect attribution | Trace a fixing commit back to the introducing change. | A `caused` edge (change→cause) populated by an SZZ-style heuristic. | Incident/defect attribution becomes a *requested* query with ground truth to validate against. | D |
| D5 | **Semantic identity resolution** | Cluster identities by semantic similarity beyond exact handles. | A semantic resolver **on top of** exact handle/email match, with a **human-review queue**. | Exact-match identity coverage measurably misses real same-person merges. | C |
| D6 | **Cross-encoder / reranker** | A learned cross-encoder re-scores fused candidates. | Optional rerank stage after RRF in `retrieval/orchestrator.py`. | The RRF-only ordering is measured as the dominant quality ceiling. | D |

> The discipline: **build ONLY on a measured trigger.** No PPR-multihop and no RAPTOR-L2 before a measured need. Each of these already has a seam (the rerank stage, the edge model, the consolidator), so deferring costs nothing structurally.

A lightweight **regex query-type classifier → per-leg RRF weights** is the one classifier-shaped item we *do* plan early (**Phase C**), precisely because it is regex (deterministic, debuggable), not a learned reranker — it tunes the existing RRF legs without adding an LLM judge.

---

## 5. EXPLICITLY REJECTED as over-engineering

> This section is prominent on purpose. These are **rejected**, documented so they are **not re-litigated**. Each is rejected because it adds a DB, a service, an engine swap, an LLM-judge loop, or a parameter sweep — without earning its keep on a single-Postgres, multi-tenant, contract-first app whose design is already at SOTA.

| # | Work / technique | Its key idea | Why it maps badly here | Verdict & reason |
|---|---|---|---|---|
| R1 | **Mem0** | Hosted/vendor agent-memory layer. | We *own* the graph (ADR D5) and it must never leave the app or enter SharedCore. | **SKIP — vendor.** Adopting it would mean shipping our crown-jewel memory to a third party and giving up RLS/contract control. |
| R2 | **Microsoft GraphRAG** (community detection / **Leiden**) | Cluster the graph into communities and summarize each. | Our retrieval is recursive-CTE + RRF on an adjacency list; community detection needs an engine we deliberately don't have. | **SKIP — over-engineering.** No community detection; the summarization win is already covered by Phase-B consolidation without clustering the whole graph. |
| R3 | **Code Property Graphs** (AST + CFG + PDG) | Fuse syntax/control/data-flow graphs for deep code analysis. | We model *people, changes, ownership, expertise over time* — not program semantics. | **SKIP — out of scope.** A CPG is a different product; it adds an enormous ingestion/representation burden for zero product-goal value. |
| R4 | **Neo4j / Apache AGE migration** | Move to a native graph engine. | ADR D13: frozen `pgvector/pgvector:pg16`; `cxa1_user` cannot `CREATE EXTENSION`; adjacency-list + recursive CTE is mandated behind the `GraphRetriever` seam. | **SKIP — no engine swap.** The seam *exists* so we *could* swap later with zero SharedCore impact — but nothing measured justifies it. |
| R5 | **RankGPT / LLM-judge reranking** | Use an LLM to judge and reorder candidates. | Non-deterministic, costly, and the **LLM never self-edits / never judges the shared graph** is a hard guardrail. | **SKIP — no LLM-judge reranking.** Confidence+recency RRF rerank (A3) is deterministic and auditable; an LLM judge would undermine both. |
| R6 | **RRF parameter sweeps** | Grid-search RRF constants per query class. | Adds tuning surface and reproducibility risk for marginal gain. | **SKIP — over-tuning.** We keep `retrieval_rrf_k=60` fixed; the regex query-type classifier (Phase C) is the bounded, deterministic alternative. |
| R7 | **Interaction-network centrality** | Rank people by social/interaction-graph centrality. | Optimizes "who is central in the social graph," not "who owns/changed this code." | **SKIP — wrong objective.** Bird/Fritz recency-decayed ownership (A5) answers the real expertise question; centrality is a distraction from the product goal. |

### 5.1 The single guardrail behind every rejection

All seven rejections fall out of one rule set, recorded here as the canonical guardrail list so future proposals can be checked against it in one pass:

- **No new DB and no new service.** (R1, R2, R3, R4)
- **No graph-engine swap; no community detection; no CPG.** (R2, R3, R4)
- **No PPR-multihop and no RAPTOR-L2 before a measured need.** (D1, D3 stay deferred)
- **No LLM-judge reranking.** (R5)
- **ONE confidence floor and ONE consolidation threshold** — no parameter zoos / sweeps. (R6)
- **The LLM never self-edits the shared graph.** (R5; consolidation writes deterministic `source='consolidation'` edges)
- **Nothing pushed into SharedCore.** (R1; SharedCore stays generic — no business logic)
- **Only additive `/v1` + MCP — no contract broken.** (all)

---

## 6. Cross-reference: verdict → phase → code

A compact index so a reader can jump from any verdict straight to the artifact.

| Verdict line | Phase | Migration | Primary code | Config keys |
|---|---|---|---|---|
| A2 supersession | A (built) | `20260614_0003__phaseA.sql` | `db/graph_repo.py` (`upsert_extracted_edge`), `edges.supersedes_edge_id` | — |
| A3/A6 confidence floor | A (built) | `0003__phaseA.sql` | `extraction/extractor.py` (`_parse_edges`) | `extraction_confidence_floor=0.6`, `confidence_floor_mode` |
| A3 rerank | A (built) | `0003__phaseA.sql` | `retrieval/orchestrator.py` (`rerank_multiplier`), `graph_repo.find_entities`/`keyword_search` | `rerank_confidence_weight=1.0`, `rerank_recency_weight=0.5`, `rerank_recency_halflife_days=90` |
| A4 activity/timeline | B (built) | `20260614_0004__phaseB.sql` | `connectors/github.py`, `graph_repo.activity_timeline`, `copilot/queries.activity`, `api/graph.py`, MCP `what_changed` | `connector_change_granularity=auto`\|`commit`\|`pr_ticket` |
| A1 consolidation | B (built) | `0004__phaseB.sql` | `extraction/consolidator.run_consolidation`, `graph_repo.consolidation_clusters`, `worker/runner.py` | `consolidation_avg_confidence=0.75`, `consolidation_min_cluster=3`, `consolidation_schedule_enabled` |
| A5 ownership / DoK | C (plan) | — | (on existing `owns`/`expert_in` + recency-decay) | — |
| D5 semantic identity | C (plan) | — | (resolver atop exact handle/email match + review queue) | — |
| regex query classifier | C (plan) | — | (per-leg RRF weights in `retrieval/orchestrator.py`) | — |
| D4 SZZ `caused` | D (gated) | — | (heuristic → `caused` edge) | — |
| D3 single-hop PPR | D (gated) | — | (reviewer recommendation) | — |
| D1 RAPTOR L2 | D (gated) | — | (meta-consolidation for >1k-edge tenants) | — |

---

## 7. Phase-A/B verification record (as built)

For the record — these are not plans:

- **A2 (supersession):** verified — on a content change the new edge links to the old via `supersedes_edge_id` **and** the old edge is closed.
- **A1 (consolidation):** verified live — produced **2** `expertise_summary` summaries; an idempotent re-run produced **0** (supersede-on-rerun holds); GRAPH-ONLY (never embedded into RAG); keyless deterministic fallback path exercised.
- **A4 (activity surface):** verified live — `POST /v1/graph/activity` and MCP `what_changed` return cited, time-ordered "who did what when."
- Triggers wired both ways: `POST /v1/extract?consolidate=true` **and** a scheduled `worker/runner.py` tick (`consolidation_schedule_enabled`), enumerating tenants from the **non-RLS outbox**.

---

## 8. Bottom line

The research did not hand us a backlog of missing capabilities; it confirmed the substrate is at 2024–2025 SOTA and pointed to **six surgical adopts** (built across Phase A/B, with A5 forward in C), **six evidence-gated defers**, and **seven explicit rejections**. The rejections are the load-bearing half of this doc: they keep cypherx-a1 from drifting into a graph-database product, an LLM-judged search product, or a vendored-memory product. Every future enhancement must clear the §5.1 guardrail list before it is even prototyped.
