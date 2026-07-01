# Phase C — sharper expertise, intent-aware retrieval, robust identity (PLANNED)

> **Documented forward plan, not yet implemented.** Three surgical, strictly-additive refinements that sharpen the copilot's two priority surfaces — *who knows/owns what* and *answering the right way per question shape* — by reusing the Phase A/B seams already in the codebase: a recency-decayed **Degree-of-Knowledge** `expert_in` + **ownership-concentration** metric computed in the consolidation/derived pass, a **lightweight regex query-type classifier** driving per-leg RRF weights, and **semantic identity resolution** with a **human-review queue** layered *on top of* the existing exact handle/email match. No new DB, no new service, no engine swap, no contract break.

---

## Where Phase C sits

Phase **A** (`db/migrations/20260614_0003__phaseA.sql`) and Phase **B** (`db/migrations/20260614_0004__phaseB.sql`) are **built and verified live** — against real Postgres, over HTTP, and through the `mcp-eng-memory` server. Phase A delivered the explicit `edges.supersedes_edge_id` supersede chain, the single `extraction_confidence_floor`, and the confidence/recency-aware rerank in `retrieval/orchestrator.py`. Phase B delivered the activity/change surface (`graph_repo.activity_timeline`, `POST /v1/graph/activity`, the MCP `what_changed` tool) and the reflection/consolidation pass (`extraction/consolidator.run_consolidation` → `expertise_summary` nodes).

Phase C is the **ADOPT-bucket** follow-on that the 5-agent / 38-work research pass placed *after* A and B: it takes the same evidence base (Bird 2011 ownership, Fritz 2010 Degree-of-Knowledge, Caul 2020) and the same consolidation seam, and turns the qualitative `expertise_summary` into **quantified, recency-decayed expertise + ownership concentration**, then sharpens retrieval routing and identity. It is gated on **product signal**, not a hard metric trigger (that gate belongs to Phase D). Nothing here ships speculatively beyond what the product goal — *"who changed what and who worked on what, over time"* — directly asks for.

> **Phase C is forward plan. Everything below describes intended behavior; none of it is in the codebase yet.** The "reuses" columns name *existing, shipped* seams Phase C would build on — not new infrastructure.

### The three items at a glance

| # | Item | Research basis | Reuses (already shipped) | Impact | Effort |
| --- | --- | --- | --- | --- | --- |
| **C1** | Recency-decayed **Degree-of-Knowledge** `expert_in` + **ownership-concentration** metric, computed in the consolidation/derived pass | Bird 2011 / Fritz 2010 / Caul 2020 | `extraction/consolidator.run_consolidation`, `graph_repo.consolidation_clusters`, the `recency = 0.5**(age_days/halflife)` decay from Phase A | Medium | Medium |
| **C2** | Lightweight **regex query-type classifier** → per-leg RRF weights | Hybrid-retrieval routing (no LLM judge) | `retrieval/orchestrator.py` RRF fusion legs (graph / RAG-dense / tsvector) | Medium | Low |
| **C3** | **Semantic identity resolution** + **human-review queue**, on top of exact match | Identity resolution (DEFER → built review-gated) | `ingestion/normalizer` identity resolution, the `identities` table | Medium | Medium–High |

---

## C1 — recency-decayed Degree-of-Knowledge `expert_in` + ownership concentration

### What

Today, Phase B's consolidation pass emits a qualitative `expertise_summary` node when a person has a high-confidence cluster of `authored`/`reviewed`/`owns`/`expert_in` edges (`avg ≥ consolidation_avg_confidence=0.75`, `count ≥ consolidation_min_cluster=3`). C1 makes that **quantitative and temporal**:

1. **Degree-of-Knowledge (DoK) score on `expert_in`** — a recency-decayed scalar per `(person, subject)` pair, derived from the volume and recency of that person's `authored`/`reviewed`/`touched`/`owns` evidence toward the subject. This is the Fritz 2010 *Degree-of-Knowledge* idea (authorship + interaction, decayed over time) folded into the existing `expert_in` edge as a derived `metadata.dok` (and an evidence count), so `who knows about X` can be **ranked**, not just listed.
2. **Ownership-concentration metric per subject** — a single scalar per repo/module/subject capturing *how concentrated* ownership is (Bird 2011 / Caul 2020 ownership: a few high-ownership contributors vs. diffuse ownership). Stored as derived metadata on the subject entity (e.g. `metadata.ownership_concentration`), so the copilot can answer *"is this a bus-factor-one module?"* and *"who really owns this?"* with a number behind it.

Both are **derived/consolidation-pass outputs** — computed from current edges, written as additive `metadata` on existing nodes/edges (plus, where warranted, a recency-decayed `expert_in` edge), never as a new table or a new kind. The recency decay reuses Phase A's exact half-life form: `decay = 0.5 ** (age_days / halflife)`.

### The existing seams it reuses

| Seam (shipped) | Role in C1 |
| --- | --- |
| `extraction/consolidator.run_consolidation` | The derived pass C1 extends — it already clusters per-person current edges and writes graph-only derived nodes idempotently. C1 adds the DoK/concentration computation inside this same pass; **no new pass, no new worker**. |
| `graph_repo.consolidation_clusters` | Already returns per-person clusters of current `authored`/`reviewed`/`owns`/`expert_in` edges with avg confidence + counts — the raw material for DoK. C1 extends the read to carry per-edge `created_at`/age so recency decay can be applied. |
| Phase A recency decay (`retrieval/orchestrator.py` rerank, `rerank_recency_halflife_days=90`) | The `0.5**(age_days/halflife)` form is reused verbatim for DoK recency weighting — one decay model across the codebase, not a second one. |
| `extraction_jobs` ledger | C1's computation is cost/run-metered and idempotent through the **existing** job ledger, exactly as consolidation is — **supersede-on-rerun** keeps derived metadata current without duplication. |
| `expert_in` edge + `metadata` JSON | DoK lands as additive `metadata` (and optionally a derived recency-decayed `expert_in` edge with `source='consolidation'`); ownership concentration lands as additive `metadata` on the subject. No schema migration required beyond what Phase B already widened. |
| `POST /v1/extract?consolidate=true` + scheduled tick (`worker/runner.py`, `consolidation_schedule_enabled`) | Both existing triggers drive C1 unchanged — it runs wherever consolidation runs. |

The copilot read side reuses the existing graph-query tools in `copilot/queries.py`; surfacing DoK/concentration in an answer is an **additive field** on existing query responses (and, if needed, an additive read on `POST /v1/graph/*`), tolerant per the additive-field convention.

### Definition of done

- DoK is computed per `(person, subject)` in `run_consolidation` from current `authored`/`reviewed`/`touched`/`owns` evidence, recency-decayed with `0.5**(age_days/halflife)`, and written as additive `metadata.dok` (+ evidence count) without creating any new table or `kind`.
- Ownership-concentration is computed per subject and written as additive `metadata.ownership_concentration` on the subject entity.
- The pass stays **idempotent + cost-metered via `extraction_jobs`** and **supersede-on-rerun**; a re-run with no new evidence produces **0** new/changed derived rows (mirrors Phase B's verified `2 → 0`).
- Output stays **GRAPH-ONLY** — DoK/concentration are never embedded into RAG and never written to Memory.
- The keyless deterministic path still works (DoK is arithmetic over edges; no provider required).
- A copilot/MCP read can **rank** `who knows about X` by DoK and report a subject's ownership concentration, cited.
- Verified the Phase A/B way: real Postgres + over HTTP + through MCP.

### Effort

**Medium.** It is arithmetic inside an existing pass over existing reads, but it touches the consolidation cluster read (to carry per-edge age), the consolidator write path, and one or two copilot read surfaces. No migration beyond additive metadata; no new service; no new trigger.

### Over-engineering guardrail

- **No new DB, no new table, no new `kind`/`rel`** — DoK and concentration are additive `metadata` on existing nodes/edges; at most one derived `expert_in` edge.
- **Reuse the single Phase A decay model** (`0.5**(age_days/halflife)`); do **not** introduce a second decay curve or a tunable per-subject half-life.
- **Exactly one consolidation threshold** stays in force — C1 does not add a parallel set of admission knobs; DoK is a derived score, not a second gate.
- **The LLM never self-edits the shared graph** — DoK/concentration are deterministic computations in the app, not LLM writes.
- **No interaction-network centrality** (explicitly SKIP) — ownership concentration is a per-subject distribution statistic, not graph centrality.
- **Graph-only** — never pushed into SharedCore RAG/Memory.

---

## C2 — lightweight regex query-type classifier → per-leg RRF weights

### What

The retrieval orchestrator fuses three legs — **graph**, **RAG-dense**, and **keyword (tsvector)** — with Reciprocal Rank Fusion, then applies the Phase A confidence/recency rerank. Today the legs are fused with fixed weighting. C2 adds a **cheap, deterministic, regex/keyword query-type classifier** that maps the incoming question to a small, closed set of *question shapes* and selects **per-leg RRF weights** accordingly. Examples of the intent → weighting intuition (the graph leg matters more for structural ownership/impact questions; dense matters more for "why/explain" questions):

| Detected query shape | Example trigger words | Leans toward |
| --- | --- | --- |
| Ownership / impact (structural) | `who owns`, `what breaks`, `depends on`, `bus factor` | **graph** leg up |
| Activity / temporal | `what changed`, `who did`, `recently`, `since`, dates | **graph** (activity) + recency |
| Rationale / explanatory | `why`, `explain`, `decision`, `rationale` | **RAG-dense** up |
| Lookup / exact | quoted strings, identifiers, file paths, symbols | **keyword/tsvector** up |

The classifier is **regex/keyword only** — no LLM call, no learned model, no parameter sweep. It is a routing heuristic in front of the existing fusion, and it falls back to the current fixed weights when no shape matches (so it can only help, never regress an unmatched query).

### The existing seams it reuses

| Seam (shipped) | Role in C2 |
| --- | --- |
| `retrieval/orchestrator.py` RRF fusion | The fusion already combines the three legs with weights — C2 makes those weights a function of the detected query shape instead of constants. **No new leg, no new index.** |
| The three existing legs (graph via `graph_repo`, RAG-dense via `rag_client`, tsvector keyword) | Unchanged; only their relative RRF contribution is tuned per query. |
| Phase A rerank (`rerank_multiplier`) | Runs **after** fusion exactly as today — C2 sits *before* the rerank and does not touch it. Recency-leaning shapes still inherit Phase A's recency decay. |
| `copilot/service` / `copilot/queries` | The classifier is invoked on the inbound question already available in the copilot flow; no new request field is required (it is derived from the query text). |
| `core/config.py` | The small set of per-shape weight vectors lives as config defaults (Doppler-overridable), consistent with how every other knob is configured. |

### Definition of done

- A deterministic regex/keyword classifier maps a question to one of a **small closed set** of shapes (plus a default/unknown shape).
- Each shape selects a per-leg RRF weight vector; the **default shape reproduces today's fixed weights exactly** (no regression for unmatched queries).
- The classifier runs in the existing copilot/retrieval path with **no extra network call** and negligible latency.
- Citations and the Phase A rerank are unchanged downstream; only fusion input weighting changes.
- Verified the Phase A/B way (real Postgres + HTTP + MCP): representative ownership/impact, temporal, rationale, and lookup questions route to the intended leaning and remain cited.

### Effort

**Low.** A pure-function classifier plus a config table of weight vectors, wired into one fusion call. No migration, no new dependency, no new endpoint.

### Over-engineering guardrail

- **No LLM-judge / RankGPT routing** (explicitly SKIP) — the classifier is regex/keyword, deterministic, and local.
- **No RRF parameter sweeps** (explicitly SKIP) — a *small, hand-set, closed* table of per-shape weight vectors, not an offline-tuned grid.
- **No cross-encoder reranker** (DEFER, Phase D territory) — C2 changes fusion *input* weighting only; the rerank stays the Phase A confidence/recency multiplier.
- **Fallback-safe** — an unmatched query uses today's exact weights, so the feature is strictly additive in behavior.
- **No new leg / no new index** — the three existing retrieval legs are untouched.

---

## C3 — semantic identity resolution + human-review queue (on top of exact match)

### What

Ingestion already performs **exact** identity resolution in `ingestion/normalizer` against the `identities` table — the same GitHub handle / email maps to the same person entity. C3 adds a **second, semantic layer on top of** that exact match: when two principals are *plausibly* the same human (e.g. `jane@old-corp.com` vs `jane.doe@new-corp.com`, or `jdoe` vs `jane-doe`) but do **not** match exactly, the system proposes a **merge candidate** rather than acting on it. Proposals land in a **human-review queue**; a reviewer approves or rejects; only an approved merge is applied to the graph.

The hard rule: **exact match is unchanged and authoritative; semantic resolution never auto-merges.** This is precisely why the research pass placed semantic identity in the **DEFER** bucket — it is built *review-gated* so a wrong guess can never silently corrupt the ownership/expertise record that the whole product depends on.

### The existing seams it reuses

| Seam (shipped) | Role in C3 |
| --- | --- |
| `ingestion/normalizer` identity resolution | The exact-match resolver stays the primary path; C3 adds a *post-exact* candidate-generation step that only fires when exact resolution does **not** already unify the principals. |
| `identities` table | Candidate merges are tracked as additive state (e.g. a `status` of `proposed` / `approved` / `rejected` in `metadata`), reusing the existing table rather than adding one. |
| `extraction_jobs` ledger + consolidation cadence | Candidate generation can run in the same metered, idempotent derived pass that Phase B/C1 already use — it is not on the hot ingest path. |
| `api/graph` + `/v1/graph/*` | The review queue (list candidates, approve, reject) is exposed as **additive `/v1` endpoints** under the existing graph router — no new service, no contract break. |
| Phase A `supersedes_edge_id` + `upsert_extracted_edge` | An **approved** merge re-points edges and closes/links superseded ones through the *existing* auditable supersede mechanism, so a merge is a first-class, reversible-in-audit graph change — not a destructive rewrite. |
| `mcp-eng-memory` invoke path | If exposed to agents at all, review actions remain reads/proposals through the additive MCP surface; the merge decision stays human. |

Semantic candidate generation may use string/normalization heuristics and (optionally, keyless-fallback-safe) embedding similarity via the existing `rag_client`/`llms_client` embeddings — but it **only proposes**; it never writes a merge.

### Definition of done

- Exact handle/email resolution is **unchanged** and remains the authoritative primary path.
- A post-exact candidate generator proposes plausible same-human merges with a similarity rationale, tracked as additive `proposed` state on `identities` (no new table).
- A **human-review queue** is exposed via additive `/v1/graph/*` endpoints (list / approve / reject); nothing merges without an explicit approval.
- An **approved** merge re-points edges via the Phase A `supersedes_edge_id` / `upsert_extracted_edge` mechanism (auditable chain), and the ownership/DoK derived metrics re-derive on the next consolidation pass.
- Candidate generation is idempotent + cost-metered via `extraction_jobs`; a re-run proposes no duplicates.
- Keyless path still functions (string/normalization heuristics work without a provider; embedding similarity is an optional enrichment).
- Verified the Phase A/B way (real Postgres + HTTP + MCP): a near-match pair is *proposed not merged*, an approval merges and re-points edges, a rejection leaves both principals intact.

### Effort

**Medium–High.** Candidate generation is moderate, but the review queue (state model, list/approve/reject endpoints, audit-correct edge re-pointing on approval) and the guarantee that a wrong guess never auto-applies make this the heaviest Phase C item.

### Over-engineering guardrail

- **Exact match stays primary and authoritative** — semantic resolution is strictly *additive on top*, never a replacement.
- **Never auto-merge** — every semantic merge passes through human review; this is the load-bearing guard that keeps the crown-jewel graph from silent corruption.
- **No new DB, no new service, no new table** — proposals reuse `identities`; the queue reuses the `/v1/graph` router; merges reuse the Phase A supersede mechanism.
- **The LLM never self-edits the shared graph** — embedding similarity may *propose*; only a human *approves*; the application performs the deterministic re-point.
- **Approved merges are auditable** via `supersedes_edge_id` (no destructive, unlinked rewrites).
- **Graph-only and SharedCore-clean** — identity logic stays in the product; nothing is pushed into SharedCore.

---

## Phase C as a whole — invariants it must hold

Phase C inherits every binding guardrail from the roadmap and adds none of its own structure:

- **No new DB and no new service.** No graph-engine swap — the frozen `pgvector/pgvector:pg16` adjacency-list + recursive-CTE graph stays; `cxa1_user` cannot `CREATE EXTENSION`.
- **No community detection / Leiden, no Code Property Graphs, no interaction-network centrality.**
- **No LLM-judge reranking, no RRF parameter sweeps, no cross-encoder** (the cross-encoder is a Phase D-class DEFER, not Phase C).
- **Exactly one confidence floor (`extraction_confidence_floor`) and exactly one consolidation threshold (`consolidation_avg_confidence` / `consolidation_min_cluster`)** stay in force — C1 adds a derived score, not a new gate; C2 adds a closed weight table, not a sweep.
- **The LLM never self-edits the shared graph** — DoK is arithmetic, classification is regex, identity merges are human-approved.
- **Reflection/derived output is graph-only** — never embedded into RAG, never written to per-principal Memory.
- **Nothing is pushed into SharedCore** — it stays generic.
- **Only additive `/v1` + MCP** — no published contract is broken.

## What Phase C is *not*

It is **not** Phase D. The evidence-gated, measured-trigger items — **SZZ** change→cause attribution into `caused` edges, **single-hop Personalized-PageRank** reviewer recommendation, and **RAPTOR L2** meta-consolidation for >1k-edge tenants — stay in [`phase-D-*.md`](./README.md) and ship **only on a measured trigger**, never as part of Phase C.

---

*Roadmap entry point: [`00-enhancement-overview-and-priorities.md`](./00-enhancement-overview-and-priorities.md). Index: [`README.md`](./README.md). Phase A & B are built + verified; Phase C (this doc) is gated on product signal; Phase D is gated on a measured trigger. Platform mental model: [`../CLAUDE.md`](../CLAUDE.md); MVP design set: [`../docs/`](../docs/).*
