# Over-engineering explicitly avoided

> The load-bearing other half of the cypherx-a1 enhancement roadmap: a single-page ledger of everything we **deliberately did not build** and **why** — no new DB or service, no Neo4j/AGE engine swap, no GraphRAG community detection, no Code Property Graphs, no LLM-judge reranking, no PPR-multihop or RAPTOR-L2 before a measured trigger, no knob proliferation (exactly **one** confidence floor + **one** consolidation threshold), the **LLM never self-edits the shared graph**, nothing pushed into SharedCore, and only additive `/v1` + MCP (**no contract broken**). Phase **A & B are built and verified live**; Phase **C & D** are forward, evidence-gated plans.

---

## 1. The guiding principle

A 5-agent research pass over 38 works concluded that cypherx-a1's substrate — a **bitemporal Postgres adjacency-list knowledge graph** (entities + typed edges) + SharedCore RAG vectors + SharedCore Memory + **hybrid RRF retrieval** + a **cited copilot** + the stateless `mcp-eng-memory` MCP server — is **already at 2024–2025 SOTA** for temporal-graph agent memory. The product goal is narrow and concrete: *every repo + Jira/issue source registers and tracks "what changed and who worked on what, over time," queryable by humans and by AI agents.*

From that conclusion follows one rule that governs this entire doc:

> **An enhancement is allowed only if it is additive, lands behind an existing seam, and earns its keep on the product goal. Anything that adds a database, a service, an engine swap, a contract break, an LLM-in-the-loop judge, or an unbounded parameter surface is over-engineering and is rejected — and recorded here so it is not re-litigated.**

This is not minimalism for its own sake. The graph is the crown jewel: **app-owned, multi-tenant RLS, never embedded into RAG, never written to per-principal Memory.** Most "advanced" graph and retrieval techniques in the literature quietly assume a graph *engine*, a *vendor*, or an *LLM judge* we deliberately do not have — and adopting any of them would convert cypherx-a1 into a different (worse, for this goal) product: a graph-database product, an LLM-judged-search product, or a vendored-memory product. The rejections below are what keep that from happening.

---

## 2. The guardrail list (binding)

Every present and future enhancement must clear all nine. They are the operationalized output of the over-engineering screen.

| # | Guardrail | Rejects |
| --- | --- | --- |
| G1 | **No new DB and no new service.** | Mem0, GraphRAG, CPG, Neo4j/AGE |
| G2 | **No graph-engine swap** — adjacency-list + recursive-CTE on frozen `pgvector/pgvector:pg16` stays. | Neo4j / Apache AGE migration |
| G3 | **No community detection and no Code Property Graphs.** | GraphRAG / Leiden, CPG (AST+CFG+PDG) |
| G4 | **No PPR-multihop and no RAPTOR-L2 before a measured need.** | HippoRAG multi-hop, RAPTOR L2 (deferred, not built) |
| G5 | **No LLM-judge reranking.** | RankGPT / LLM-judge reorder |
| G6 | **One** confidence floor and **one** consolidation threshold — no knob proliferation. | RRF parameter sweeps, per-class tuning zoos |
| G7 | **The LLM never self-edits the shared graph.** | any "agent rewrites memory" loop |
| G8 | **Nothing is pushed into SharedCore** — it stays generic, no business logic. | Mem0; any SharedCore feature creep |
| G9 | **Only additive `/v1` + MCP — no contract broken.** | any breaking schema/route change |

---

## 3. The substrate that pre-empts whole classes of "additions"

Several would-be enhancements are unnecessary not because they are bad ideas in general, but because the capability they chase **already exists** in the as-built MVP. Recording this prevents re-proposing them as gaps.

| Already in the substrate | Where it lives | Pre-empts |
| --- | --- | --- |
| Bitemporal graph (valid-time + system-time; current-edge closing) | `db/graph_repo.py`; `db/migrations/20260614_0003__phaseA.sql` | "Add a temporal/contradiction layer" — Zep/Graphiti's headline feature is the *substrate*, not a bolt-on |
| Adjacency list + recursive CTE (no Apache AGE) | `db/graph_repo.py` (`find_entities`, `keyword_search`); ADR D13 | "Migrate to a graph engine" — frozen `pgvector/pg16`, `cxa1_user` cannot `CREATE EXTENSION` |
| Hybrid RRF retrieval (dense RAG ⊕ keyword tsvector) | `retrieval/orchestrator.py` (`retrieve`, `retrieval_rrf_k=60`) | "Add a fusion layer" — the fusion seam where signals plug in already exists |
| App-owned graph, never embedded into RAG/Memory | ADR D5; `ingestion/pipeline.py` | The leakage/cost failure mode that motivates several rejected designs |
| Cited copilot + stateless `mcp-eng-memory` | `copilot/service.py`; `mcp-eng-memory/` (ADR D12) | "Add provenance" — citations are first-class already |

---

## 4. SKIP — explicitly rejected as over-engineering

These are **rejected outright**, not deferred. Each adds a DB, a service, an engine swap, an LLM-judge loop, a vendor dependency, or a parameter sweep — without earning its keep on a single-Postgres, multi-tenant, contract-first app whose design is already at SOTA.

| # | Technique | Its key idea | Why it maps badly here | Guardrail(s) |
| --- | --- | --- | --- | --- |
| R1 | **Mem0** | Hosted/vendor agent-memory layer. | We *own* the graph (ADR D5); it must never leave the app or enter SharedCore. Adopting it ships our crown-jewel memory to a third party and gives up RLS/contract control. | G1, G8 |
| R2 | **Microsoft GraphRAG** (community detection / **Leiden**) | Cluster the graph into communities, summarize each. | Retrieval is recursive-CTE + RRF over an adjacency list; community detection needs an engine we deliberately don't have. The summarization win is already covered by Phase-B consolidation *without* clustering the whole graph. | G2, G3 |
| R3 | **Code Property Graphs** (AST + CFG + PDG) | Fuse syntax/control/data-flow graphs for deep program analysis. | We model *people, changes, ownership, expertise over time* — not program semantics. A CPG is a different product with an enormous ingestion/representation burden and zero product-goal value. | G3 |
| R4 | **Neo4j / Apache AGE migration** | Move to a native graph engine. | ADR D13: frozen `pgvector/pgvector:pg16`; `cxa1_user` cannot `CREATE EXTENSION`; adjacency-list + recursive CTE is mandated behind the `GraphRetriever` seam. The seam exists so we *could* swap later with zero SharedCore impact — but nothing measured justifies it. | G2 |
| R5 | **RankGPT / LLM-judge reranking** | Use an LLM to judge and reorder candidates. | Non-deterministic, costly; and **the LLM never judges or self-edits the shared graph** is a hard guardrail. Confidence+recency RRF rerank (Phase A) is deterministic and auditable; an LLM judge would undermine both. | G5, G7 |
| R6 | **RRF parameter sweeps** | Grid-search RRF constants per query class. | Adds tuning surface and reproducibility risk for marginal gain. We keep `retrieval_rrf_k=60` fixed; the Phase-C regex query-type classifier is the bounded, deterministic alternative. | G6 |
| R7 | **Interaction-network centrality** | Rank people by social/interaction-graph centrality. | Optimizes "who is central in the social graph," not "who owns/changed this code." Bird/Fritz recency-decayed ownership (Phase C) answers the real expertise question. | (wrong objective) |

### 4.1 Why each rejection is safe to make once and never revisit

The rejections are not judgment calls per proposal — they fall out of the §2 guardrails mechanically. A future proposal that resembles R1–R7 fails the same guardrail it failed the first time. The point of writing them down is to make that check **a one-pass lookup**, not a re-debate.

---

## 5. DEFER — not skipped, but deliberately *not built yet*

These are sound techniques whose cost is only justified by a **measured** trigger. Building one early — speculatively, before the trigger fires — would itself be the over-engineering we are avoiding. They are designed-for behind an existing seam (the rerank stage, the edge model, the consolidator) so deferring costs nothing structurally. **No PPR-multihop and no RAPTOR-L2 before a measured need** is the headline discipline (G4).

| # | Technique | Where it would land | Build ONLY on this trigger | Phase |
| --- | --- | --- | --- | --- |
| D1 | **RAPTOR L2** meta-consolidation | A second consolidation tier over Phase-B `expertise_summary` nodes (summaries-of-summaries). | A tenant exceeds **>1k edges** *and* single-level consolidation demonstrably under-summarizes. | D |
| D2 | **A-MEM** active reinforcement | A reinforcement signal on edge confidence upon repeated corroboration. | Measured drift between stored confidence and observed corroboration frequency. | D |
| D3 | **HippoRAG** Personalized-PageRank | A **single-hop** PPR reviewer-recommendation only — never full multi-hop. | Recursive-CTE neighborhood proves insufficient for a real reviewer-rec query. | D |
| D4 | **SZZ** defect attribution | A `caused` edge (change→cause) from an SZZ-style heuristic. | Incident/defect attribution becomes a *requested* query with ground truth to validate against. | D |
| D5 | **Semantic identity resolution** | A semantic resolver **on top of** exact handle/email match, with a **human-review queue**. | Exact-match identity coverage measurably misses real same-person merges. | C |
| D6 | **Cross-encoder / learned reranker** | Optional rerank stage after RRF in `retrieval/orchestrator.py`. | RRF-only ordering is measured as the dominant quality ceiling. | D |

> The one classifier-shaped item we *do* plan early (**Phase C**) is a lightweight **regex** query-type classifier → per-leg RRF weights — precisely *because* it is regex (deterministic, debuggable), not a learned reranker. It tunes the existing RRF legs without adding an LLM judge (clears G5) and without a parameter sweep (clears G6).

---

## 6. What we built instead — the surgical alternatives (Phase A & B, built + verified)

For every tempting over-engineered path there is a constrained, in-seam move that captures the *signal* without the *machinery*. These are **shipped and verified live** against real Postgres, over HTTP, and over MCP.

| Over-engineered path rejected | Surgical alternative we built | Where (built) |
| --- | --- | --- |
| Engine migration for a "real" temporal/contradiction graph | Additive column **`edges.supersedes_edge_id`**; `graph_repo.upsert_extracted_edge` closes the prior *current* edge on a content change and links the new edge to it — an auditable contradiction chain on the existing adjacency list. **Verified:** new→old link present **and** old edge closed. | `db/migrations/20260614_0003__phaseA.sql`, `db/graph_repo.py::upsert_extracted_edge` |
| MemGPT memory-tiering (a paging "memory OS") | **ADAPT — precedence, not tiering.** Confidence + recency folded into the RRF rerank: `orchestrator.rerank_multiplier = (1 + w_conf*confidence) * ((1 - w_rec) + w_rec*recency_decay)`, `recency = 0.5**(age_days/halflife)`. No new index, no LLM judge. | `retrieval/orchestrator.py`; `graph_repo.find_entities`/`keyword_search` (strongest **current** edge confidence + `created_at`) |
| A parameter zoo governing what enters the graph + how it ranks | **Exactly one floor in, one precedence rank out** (G6). `extractor._parse_edges` enforces a single `extraction_confidence_floor=0.6` with `confidence_floor_mode=flag|drop`. | `extraction/extractor.py::_parse_edges` |
| GraphRAG community detection / Leiden for summarization | **Phase-B reflection/consolidation** without clustering the whole graph: `consolidator.run_consolidation` clusters each person's current `authored`/`reviewed`/`owns`/`expert_in` edges → an `expertise_summary` node + `summarizes` evidence edges. **Verified live:** 2 summaries; idempotent re-run → 0. | `extraction/consolidator.run_consolidation`, `graph_repo.consolidation_clusters`, `worker/runner.py` |
| LLM self-editing the shared graph | **Deterministic, graph-only writes** (G7): consolidation emits `summarizes` edges with `source='consolidation'`, idempotent + cost-metered via `extraction_jobs`, supersede-on-rerun, with a **keyless deterministic fallback**. **Never embedded into RAG, never written to Memory.** | `extraction/consolidator.py` |
| A bespoke change-tracking store/service | Commit-level **`change`** nodes (`authored` + `touched` edges) emitted by the existing connector; a time-ordered `activity_timeline(scope_entity_id, since, until)`; surfaced via additive `POST /v1/graph/activity` and the MCP `what_changed` tool. **Verified live:** cited, time-ordered "who did what when." | `connectors/github.py`, `graph_repo.activity_timeline`, `copilot/queries.activity`, `api/graph.py`, `mcp-eng-memory` manifest + invoke |

The Phase-B schema widening that enabled the above stayed **additive** (G9): `db/migrations/20260614_0004__phaseB.sql` widened the `kind` CHECK (`+change`, `+capability`, `+expertise_summary`) and the `rel` CHECK (`+touched`, `+summarizes`), and added an activity index. The granularity is a **single configurable knob** — `connector_change_granularity=auto|commit|pr_ticket` — not a hard-coded policy and not a tuning surface.

### 6.1 The exactly-two knobs

The whole point of G6 is that there is no parameter zoo. The complete tuning surface introduced by Phase A & B is:

| Knob | Default | Governs |
| --- | --- | --- |
| `extraction_confidence_floor` (+ `confidence_floor_mode=flag\|drop`) | `0.6` | The **one** floor: what enters the graph. |
| `consolidation_avg_confidence` / `consolidation_min_cluster` | `0.75` / `3` | The **one** consolidation threshold pair: what becomes an `expertise_summary`. |

Everything else (`retrieval_rrf_k=60`, the rerank weights `rerank_confidence_weight=1.0`, `rerank_recency_weight=0.5`, `rerank_recency_halflife_days=90`) is **fixed**, not swept.

---

## 7. The boundaries we did not cross

A compact restatement of what stayed off-limits, with the reason each boundary is load-bearing.

| Boundary held | Why it is load-bearing |
| --- | --- |
| **No new DB / no new service** | cypherx-a1 is one FastAPI product service + one stateless MCP facade. Adding storage or a service would multiply the RLS/migration/operational surface for zero product-goal gain. |
| **No graph-engine swap** | The `GraphRetriever` seam means a *future* AGE/Neo4j swap (if ever measured-justified) touches no SharedCore. Crossing the boundary now buys nothing measured and forfeits the frozen-image guarantee. |
| **No community detection / no CPG** | Both require representations and engines we don't have; neither serves "who changed/owns what, over time." |
| **No PPR-multihop / no RAPTOR-L2 (pre-trigger)** | Deferred behind explicit triggers (>1k-edge tenants; demonstrated recursive-CTE insufficiency). Speculative builds here are the textbook over-engineering case. |
| **No LLM-judge reranking** | Determinism and auditability of retrieval are guarantees we will not trade for marginal ranking gains. |
| **The LLM never self-edits the shared graph** | Consolidation writes are deterministic `source='consolidation'` edges with a keyless fallback. The shared graph must remain a trustworthy, non-hallucinated record. |
| **Nothing in SharedCore** | SharedCore (auth, llms, guardrails, rag, memory) stays generic — consumed only through versioned `/v1` contracts. All domain logic lives in cypherx-a1. |
| **No broken contracts** | Every change is additive `/v1` + MCP. Contracts 1/2/4/5/6/7/8/9/12/13/14/19 are honoured, never re-shaped. |

---

## 8. How to use this doc

When a new enhancement is proposed — by a human or by an AI agent reading this repo — run it through §2 (G1–G9) in one pass **before any prototype**:

1. Does it add a DB, a service, an engine, or a vendor? → **reject** (G1, G2, G8).
2. Does it need community detection, a CPG, or a graph engine? → **reject** (G2, G3).
3. Does it put an LLM in the judging/self-editing loop over the shared graph? → **reject** (G5, G7).
4. Does it add a parameter sweep or a second floor/threshold? → **reject** (G6).
5. Does it break a `/v1` or MCP contract? → **reject** (G9).
6. Is it PPR-multihop or RAPTOR-L2 *without* a measured trigger? → **defer** (G4), do not build.
7. Otherwise — is it additive, behind an existing seam, and justified on the product goal? → it may proceed as a surgical win.

If a proposal matches an R-line in §4, it is already decided: **rejected**. If it matches a D-line in §5, it is already decided: **deferred until its named trigger fires.** This doc exists so those decisions are made once.

---

## 9. Bottom line

The research pass did not hand cypherx-a1 a backlog of missing capabilities — it confirmed the substrate is at 2024–2025 SOTA and that the wins are surgical. The disciplined half of that conclusion is everything we **chose not to build**: no new DB or service, no Neo4j/AGE, no GraphRAG community detection, no Code Property Graphs, no LLM-judge reranking, no PPR-multihop or RAPTOR-L2 before a measured trigger, exactly one confidence floor and one consolidation threshold, no LLM self-editing the shared graph, nothing in SharedCore, and no broken contracts. These rejections are what keep the Autonomous Engineering Memory focused on its one job — and they are recorded here so they are never re-litigated.

---

*Companion docs in `phases/`: [`00-enhancement-overview-and-priorities.md`](00-enhancement-overview-and-priorities.md) (the roadmap entry point), [`02-research-verdict-and-rejections.md`](02-research-verdict-and-rejections.md) (the full adopt/adapt/defer/skip ledger over 38 works), [`phase-A-confidence-weighted-bitemporal-retrieval.md`](phase-A-confidence-weighted-bitemporal-retrieval.md), [`phase-B-reflection-and-activity-surface.md`](phase-B-reflection-and-activity-surface.md). Platform mental model: [`../CLAUDE.md`](../CLAUDE.md).*
