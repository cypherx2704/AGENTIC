# Phase D — optional causation chains & scale-only abstractions (EVIDENCE-GATED)

> The deferred tail of the cypherx-a1 enhancement roadmap: three higher-cost techniques — **SZZ change→cause attribution**, **single-hop Personalized-PageRank reviewer recommendation**, and **RAPTOR L2 meta-consolidation** — each documented in full but **built only when a concrete, measured trigger fires.** Nothing here is built speculatively; until its gate trips, every item is a skip.

---

## What Phase D is (and what it is not)

Phase A and Phase B are **shipped and verified live** (against real Postgres, over HTTP, and through `mcp-eng-memory`) — they delivered bi-temporal supersede + confidence/recency rerank, the activity/change surface, and the reflection/consolidation pass. Phase C is a **forward plan** gated on product signal. **Phase D is the evidence-gated tail**: the three techniques the 5-agent / 38-work research pass scored as *sound but unproven for this product at current scale*.

Phase D exists for one reason: to make the **deferral decision auditable**. Each item carries a precise, measurable trigger so that:

1. it is **never built on a hunch** — the gate is a number, measured from production telemetry, not an opinion; and
2. when the gate *does* trip, the design is already written and screened against the over-engineering guardrails, so the build is a small additive change, not a re-think.

> **The default state of every Phase D item is SKIP.** Promotion to BUILD requires the named metric to cross the named threshold, measured over the named window. Absent that, the over-engineering screen (no new DB, no engine swap, no community detection/CPG, no PPR-multihop or RAPTOR-L2 before a measured need) holds the line and these stay unbuilt.

Phase D inherits every binding guardrail from doc `00`: **no new DB, no new service, no graph-engine swap** (adjacency-list + recursive-CTE on the frozen `pgvector/pgvector:pg16` image — `cxa1_user` cannot `CREATE EXTENSION`), **no community detection / Code Property Graphs, no LLM-judge reranking, only additive `/v1` + MCP, the LLM never self-edits the shared graph, and nothing is pushed into SharedCore.**

---

## The three deferred items at a glance

| # | Item | Research basis | Default state | Promotes to BUILD when… |
| --- | --- | --- | --- | --- |
| D1 | **SZZ** change→cause attribution → `caused` edges | SZZ defect attribution (Śliwerski/Zimmermann/Zeller 2005) | **SKIP** | a measured **incident-RCA miss rate** exceeds the gate |
| D2 | **Single-hop Personalized-PageRank** reviewer recommendation | HippoRAG PPR (single-hop variant only) | **SKIP** | a measured **multi-hop reviewer-routing miss rate** exceeds the gate |
| D3 | **RAPTOR L2** meta-consolidation | RAPTOR hierarchical summarization | **SKIP** | a tenant crosses **>1k edges** *and* single-level (Phase B) consolidation measurably underperforms |

Each is detailed below: the user problem, the proposed minimal design (additive only), the **concrete measured trigger**, the measurement method, and the explicit reasons it is deferred rather than built now.

---

## D1 — SZZ change→cause attribution into `caused` edges

### The problem it would solve

The Phase B activity/change surface already records *what changed and who did it* (`change` nodes with `authored` + `touched` edges, queryable via `activity_timeline` and the `what_changed` MCP tool). What it does **not** record is *which earlier change introduced a defect that a later change fixed* — the causal link from a fix back to its root-cause commit. SZZ (Śliwerski–Zimmermann–Zeller) is the canonical algorithm for this: given a bug-fixing change, walk the blame/diff of the lines it touched back to the change(s) that last modified those lines, and attribute cause.

### Proposed minimal design (additive only)

| Aspect | Decision |
| --- | --- |
| New relationship | A `caused` edge `change(root-cause) → change(fix)` (or `change → incident`), additive to the `rel` CHECK exactly as Phase B widened it for `touched`/`summarizes`. **No new node kind.** |
| Where it runs | In the extraction/consolidation tier (alongside `extraction/consolidator.run_consolidation`), **never** as an LLM self-edit of the shared graph. SZZ is a deterministic blame-walk over already-ingested `change`/`touched` edges. |
| Inputs | Existing `change` nodes + their `touched` edges + the fix↔incident linkage already surfaced by the MSR issue–commit linking adopted in Phase B. No new ingestion source required for the base variant. |
| Confidence | Each `caused` edge carries a confidence subject to the **single** `extraction_confidence_floor` (0.6) and `confidence_floor_mode` already in `extraction/extractor.py::_parse_edges`. No second floor knob. |
| Bi-temporality | `caused` edges supersede via the existing `edges.supersedes_edge_id` chain (`db/graph_repo.py::upsert_extracted_edge`) when a later, better attribution replaces an earlier one. |
| Surface | Read-only exposure through the existing copilot graph tools (`copilot/queries.py`) and an additive MCP read — answering *"what introduced this defect?"* — **no contract break.** |

### The concrete measured trigger

> **BUILD D1 only when the incident-RCA miss rate exceeds the gate.**

| Trigger element | Value |
| --- | --- |
| Metric | **incident-RCA miss rate** = fraction of incident/root-cause copilot questions (e.g. *"what change caused this regression?"*) that return **no cited causal answer** the asker accepts. |
| Gate | Miss rate **> 30%** sustained over a **rolling 30-day** window with **≥ 20 such questions** in-window (below that count it is noise, not a signal). |
| Measurement | From copilot/MCP query telemetry already emitted on the product's own `cypherx.cypherxa1.usage.recorded` topic, segmented by question intent (the Phase C regex query-type classifier, once it lands, supplies the intent label; until then, a manual tag on RCA-shaped questions). A "miss" = no `caused`/causal citation returned, or an explicit unhelpful-result signal. |

### Why it is deferred, not built now

- **No measured demand yet.** Without the miss-rate signal, building SZZ adds a blame-walk subsystem and a `caused` edge type that may answer questions nobody asks.
- **SZZ is noisy.** Naïve SZZ over-attributes (cosmetic/whitespace changes, refactors). Building it before there is demand means tuning a noisy heuristic with no ground truth to tune against — the in-window RCA questions *are* the ground truth that makes the build worthwhile.
- **The Phase B surface may already suffice.** A human can often answer "what caused this" from the activity timeline + supersede chain. The gate exists precisely to confirm the timeline is *not* enough before adding causal inference.

---

## D2 — single-hop Personalized-PageRank reviewer recommendation

### The problem it would solve

*"Who should review this change?"* is an ownership-and-expertise routing question. Phase B's `expertise_summary` nodes and Phase C's planned recency-decayed Degree-of-Knowledge / ownership-concentration metric answer the **direct** case well: the person who owns or is expert in the touched files. The harder case is **indirect** routing — the best reviewer is one relationship-hop away (owns a dependency, reviewed the adjacent module, authored the superseded design) and so does not surface from a direct ownership lookup. HippoRAG's Personalized-PageRank is the research basis for ranking graph neighbours by personalized relevance; **single-hop** PPR is the bounded, scale-safe subset of it.

### Proposed minimal design (additive only)

| Aspect | Decision |
| --- | --- |
| Algorithm | **Single-hop** Personalized-PageRank: seed the personalization vector from the touched entities, take **one** relationship expansion over the existing adjacency-list edges, rank neighbours by the seeded score. **No multi-hop PPR** — that is explicitly out of bounds per the guardrails. |
| Implementation | A bounded recursive-CTE / single-hop expansion in `db/graph_repo.py`, reusing the existing adjacency-list reads. **No graph engine, no PPR library, no precomputed centrality table.** |
| Inputs | Current `owns` / `authored` / `reviewed` / `expert_in` / `touched` edges, weighted by the Phase A confidence + recency the rerank already exposes (the *strongest current edge confidence* correlated-subquery surfaced by `find_entities`/`keyword_search`). |
| Surface | A read-only "suggest reviewers" result through `copilot/queries.py` + an additive MCP read. Suggestions are **advisory and cited** (each candidate carries its evidence edges); the LLM never writes a reviewer assignment back into the graph. |
| Boundary | Graph-only and stateless on the MCP side — `mcp-eng-memory` stays a stateless facade; the ranking runs in the cypherx-a1 backend. |

### The concrete measured trigger

> **BUILD D2 only when the multi-hop reviewer-routing miss rate exceeds the gate — i.e. when the *direct* recommendation demonstrably falls short.**

| Trigger element | Value |
| --- | --- |
| Metric | **multi-hop miss rate** = fraction of reviewer-recommendation queries where the **direct** (Phase C ownership/expertise) answer is judged insufficient *and* the correct reviewer is provably one hop away in the graph. |
| Gate | Miss rate **> 25%** over a **rolling 30-day** window with **≥ 20** reviewer-recommendation queries in-window. |
| Measurement | Reviewer-recommendation queries are tagged by the query-type classifier (Phase C). A "multi-hop miss" is logged when the direct recommendation is rejected/overridden and the accepted reviewer is reachable in exactly one hop from the touched entities (a cheap post-hoc graph check). The single-hop PPR build is justified **only** if these misses are concentrated at one hop — if the gap is at two-plus hops, the guardrail against PPR-multihop still holds and D2 stays skipped. |

### Why it is deferred, not built now

- **Direct ownership likely covers most cases.** Phase B/C already answer the common "who owns this" routing question. PPR only earns its keep if the *indirect* case is both frequent and one-hop — the gate measures exactly that.
- **PPR invites scope creep.** Multi-hop PPR is a known over-engineering trap at this scale and is explicitly skipped. Confining D2 to **single-hop** and gating it on a *one-hop* miss signal keeps the algorithm bounded and the guardrail intact.
- **No engine swap to justify.** Running even single-hop PPR must stay inside adjacency-list + recursive-CTE on `pgvector/pg16`. If a measured need ever pushed toward multi-hop, that is a *separate* future decision, not an automatic consequence of building D2.

---

## D3 — RAPTOR L2 meta-consolidation for >1k-edge tenants

### The problem it would solve

Phase B's reflection/consolidation pass (`extraction/consolidator.run_consolidation`) is **single-level**: it clusters each person's current `authored`/`reviewed`/`owns`/`expert_in` edges and, when a cluster clears the **single** consolidation threshold (`consolidation_avg_confidence` 0.75, `consolidation_min_cluster` 3), emits one `expertise_summary` node + `summarizes` evidence edges. For a small or mid-size tenant this single level is the right altitude. For a **very large** tenant — thousands of edges, hundreds of summaries — the *summaries themselves* become numerous enough that a second, higher-level pass (RAPTOR's L2: summarize the summaries) could improve retrieval recall and copilot answer quality. RAPTOR is the research basis for this recursive, tree-structured summarization.

### Proposed minimal design (additive only)

| Aspect | Decision |
| --- | --- |
| What it adds | A **second** consolidation level that clusters and summarizes existing `expertise_summary` nodes into higher-level summaries, reusing the **same** node kind and `summarizes` relationship (additive, no new kind). |
| Reuse | Runs in `extraction/consolidator` alongside `run_consolidation`, with the **same** idempotency, cost-metering via `extraction_jobs`, supersede-on-rerun, **GRAPH-ONLY** boundary (never embedded into RAG, never written to Memory), and keyless deterministic fallback. |
| Threshold | Reuses the **single** existing consolidation threshold pair — **no new knob.** The guardrail "one confidence floor and one consolidation threshold — no proliferating knobs" is binding. |
| Scope guard | Only ever runs for tenants past the edge-count gate; for everyone else it does not exist. The per-tenant worker tick (`worker/runner.py`, enumerating tenants from the non-RLS `outbox`) is where the edge-count check would live. |
| Triggers | Same two triggers as Phase B consolidation: on-demand `POST /v1/extract?consolidate=true` and the scheduled worker tick (`consolidation_schedule_enabled`) — Phase D adds no new trigger surface. |

### The concrete measured trigger

> **BUILD D3 only for tenants with >1k edges, AND only where single-level consolidation measurably underperforms.** Both conditions are required; scale alone is not enough.

| Trigger element | Value |
| --- | --- |
| Condition 1 (scale) | A tenant's current-edge count **> 1,000** (measured from the graph, per tenant). |
| Condition 2 (underperformance) | For those tenants, the single-level `expertise_summary` layer shows a measured deficiency — e.g. the number of distinct `expertise_summary` nodes per person grows past a usable point (summaries no longer summarize), reflected as a degraded expertise/ownership answer-acceptance rate on copilot queries over a **rolling 30-day** window. |
| Measurement | Edge count is a direct query. Underperformance is read from the product's own usage/answer telemetry, segmented to expertise/ownership questions for >1k-edge tenants. A second level is justified **only** when both conditions hold together. |

### Why it is deferred, not built now

- **No tenant is at the scale yet.** RAPTOR L2 is a *scale-only* abstraction. Building a recursive summarization tier before any tenant crosses 1k edges adds cost and surface for zero current benefit.
- **Single-level may stay sufficient even past 1k.** Crossing the edge count is **necessary but not sufficient** — condition 2 guards against building L2 for a large tenant whose single-level summaries are still doing their job.
- **Recursion is the expensive part.** A second consolidation level multiplies extraction cost (metered via `extraction_jobs`) and adds idempotency/supersede surface. Gating it on a measured under-performance signal ensures that cost is only ever paid where it demonstrably buys answer quality.

---

## Decision flow — how a Phase D item moves from SKIP to BUILD

```
            ┌─────────────────────────────────────────────┐
            │  Default state of D1 / D2 / D3 = SKIP        │
            └─────────────────────────────────────────────┘
                              │
              measure the named metric over its window
                              │
                ┌─────────────┴─────────────┐
                │                           │
        gate NOT crossed              gate crossed
                │                           │
         stay SKIP (guardrails        re-screen the minimal
         hold; nothing built)         additive design vs.
                                      over-engineering rules
                                            │
                                  ┌─────────┴─────────┐
                                  │                   │
                          still over-engineered   passes screen
                                  │                   │
                              stay SKIP          BUILD (additive
                                                 /v1 + MCP only)
```

The gate is a **gate, not a green light**: crossing the threshold makes the item *eligible*, and it must still pass the over-engineering re-screen (no new DB/service, no engine swap, no multi-hop PPR, no second threshold knob, graph-only, nothing into SharedCore) before any code is written.

---

## Phase D guardrails (binding, inherited from doc `00`)

These hold whether or not any Phase D item is ever built:

- **No new DB and no new service** — D1/D2/D3 all run inside the existing FastAPI product service + its `cypherx_a1` schema.
- **No graph-engine swap** — single-hop PPR and SZZ blame-walks stay on adjacency-list + recursive-CTE on frozen `pgvector/pg16`; `cxa1_user` cannot `CREATE EXTENSION`.
- **No multi-hop PPR and no RAPTOR-L2 before a measured need** — D2 is single-hop only; D3 is scale-gated *and* underperformance-gated.
- **No community detection and no Code Property Graphs** — never in scope.
- **No LLM-judge reranking** — the rerank stays the Phase A confidence/recency formula.
- **One confidence floor and one consolidation threshold** — D1 reuses `extraction_confidence_floor`; D3 reuses the Phase B consolidation threshold pair. No new knobs.
- **The LLM never self-edits the shared graph** — `caused` edges (D1) and reviewer ranks (D2) are produced by deterministic graph computation; suggestions are advisory and cited.
- **Nothing is pushed into SharedCore** — it stays generic.
- **Only additive `/v1` + MCP** — every Phase D surface is a new read, never a contract break. Consolidation output and `caused` edges remain **GRAPH-ONLY** (never embedded into RAG, never written to Memory).

---

## How to read this doc

This is the Phase D doc of the cypherx-a1 enhancement roadmap (`phases/`). It is the **evidence-gated tail** following [`00-enhancement-overview-and-priorities.md`](00-enhancement-overview-and-priorities.md). Phase A & B are **shipped and verified live**; Phase C is a forward plan gated on product signal; **Phase D is forward and built only on a measured trigger.** The single most important property of this doc is that each item names the concrete metric, threshold, and window that would promote it from **SKIP** to **BUILD** — so the deferral is auditable and the build, if it ever happens, is a small additive change already screened against the guardrails. For the platform mental model and the SharedCore boundary this respects, see [`../CLAUDE.md`](../CLAUDE.md).
