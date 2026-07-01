# How the KB is built today

> The end-to-end path a source artifact takes to become queryable engineering memory: **connector → canonical → landing (`raw_events`) → normalize (graph upsert + identity resolution) → RAG ingest → LLM extraction (confidence floor + supersede chain) → hybrid RRF retrieval (graph-aware rerank) → cited copilot + the deterministic graph-query surface + the activity timeline.** Phases **A** and **B** are implemented and verified live; Phases **C** and **D** are forward plans.

This document is the precise, code-grounded walkthrough of the current cypherx-a1 pipeline as it stands on `development`. Every table, column, function, config key, and endpoint quoted below is real and load-bearing — file paths are given so each claim is checkable. The product goal is to make scattered engineering history (every repo + Jira/etc.) queryable as "what changed and who worked on what, over time", by humans and AI agents alike, with citations on every answer.

---

## 0. The shape of the system

cypherx-a1 is a first-class CypherX **consuming app** (peer of `xAgent/ax-1`), not a SharedCore service. It owns ALL domain logic and the `cypherx_a1` Postgres schema, and it reuses SharedCore (auth, llms, guardrails, rag, memory) strictly through versioned `/v1` contracts — **no business logic is pushed into SharedCore**.

The knowledge base is a **layered combination**, not one store:

| Layer | Lives in | Owned by | Holds |
| --- | --- | --- | --- |
| **Graph** (the crown jewel) | `cypherx_a1.entities` + `cypherx_a1.edges` | app (this repo) | bitemporal adjacency-list nodes + typed edges; traversed by recursive CTEs |
| **Vectors** | SharedCore RAG knowledge bases | RAG service | opaque text + provenance metadata; this app stores only a `vector_ref` |
| **Conversational memory** | SharedCore Memory | Memory service | per-principal copilot memory; the graph never enters it |

Hard architectural constraints that shape everything below:

- **No Apache AGE / ltree / graph engine.** The image is frozen `pgvector/pgvector:pg16`; the runtime role `cxa1_user` is `LOGIN`, **not** superuser, **no** `BYPASSRLS`, and **cannot `CREATE EXTENSION`**. The graph is therefore an adjacency list traversed by recursive CTEs (`db/migrations/20260614_0001__init.sql`).
- **Multi-tenant RLS (Contract 13).** Every tenant-scoped table has `ENABLE` + `FORCE ROW LEVEL SECURITY` with a policy keyed on `current_setting('app.tenant_id', true)`. The repo layer (`src/cypherx_a1/db/graph_repo.py`) never opens its own transaction or sets the GUC — it always runs inside the `in_tenant(pool, tenant_id, fn)` helper.
- **Identity from the JWT only.** `tenant_id` / `agent_id` come from the verified token, never a request body; wire models are `extra="forbid"`.
- **The graph is app-owned and never embedded.** It does not enter RAG and does not enter Memory.

---

## 1. Connector → Canonical

A connector turns one source record (a commit, PR, issue, message, …) into a **`CanonicalRecord`** — the single shape every connector normalizes to (`src/cypherx_a1/models/canonical.py`):

```
CanonicalRecord = { source, record_type, external_id, content_sha,
                    nodes: [CanonicalNode], edges: [CanonicalEdge], docs: [RagDoc] }
```

- **`CanonicalNode`** — a graph node referenced by a stable `(kind, natural_key)` pair, so an edge can wire two nodes together before either has a DB UUID. `natural_key` is the dedup key within `(tenant, kind)` (a repo's `owner/name`, a person's canonical email, a PR's `repo#number`). Person nodes carry `identity_handles: [(source, handle)]` for cross-tool identity resolution.
- **`CanonicalEdge`** — a typed relationship `(rel, src: NodeRef, dst: NodeRef, confidence, metadata)`.
- **`RagDoc`** — text to embed into a named logical KB (`eng-code` / `eng-conversations` / `eng-docs` / `eng-incidents`), linked back to its node.

The vocabulary widened in Phase B (see `EntityKind` / `EdgeRel` literals): kinds now include `change`, `capability`, `expertise_summary`; relations include `touched` and `summarizes`.

### The GitHub connector (`src/cypherx_a1/connectors/github.py`)

GitHub is the MVP connector, with two modes selected by `CONNECTOR_MODE`:

- **`mock`** (default, keyless local) — replays a small bundled fixture repo (`_fixture_records`) so the whole `ingest → graph → RAG → copilot` path runs end-to-end with no GitHub token. The fixtures **deliberately include explicit `owns` / `depends_on` edges** so `who_owns` / `what_breaks_if_changed` work without any LLM.
- **`live`** — calls the GitHub REST API (best-effort first cycle: pulls + issues) and verifies real `X-Hub-Signature-256` webhook signatures.

Each record type maps to a builder: `_repo_record` (repo node), `_pr_record` (a `pr` node + `authored`/`reviewed`/`part_of` edges + an `eng-code` `RagDoc`), `_issue_record` (a `ticket` node + `eng-docs` doc), and the **Phase B `_change_record`** — a discrete commit-level `change` event:

```python
change = CanonicalNode(kind="change", source="github", natural_key=f"{full_name}@{sha}",
                       title=message, attrs={"sha", "message", "timestamp", "files", "url", ...})
edges  = [authored(author -> change), touched(change -> repo)]
```

Whether commit-level `change` events are emitted is governed by **`connector_change_granularity`** (`auto` | `commit` | `pr_ticket`, default `auto`): under `auto`/`commit` the fixtures (and live syncs) include commit `change` nodes; `pr_ticket` falls back to PR/ticket-transition granularity.

---

## 2. Landing → `raw_events`

`src/cypherx_a1/ingestion/pipeline.py` (`ingest_records` → `_ingest_one`) processes each canonical record. **Step 1 is an idempotent landing** into the immutable `raw_events` table:

| `raw_events` column | Role |
| --- | --- |
| `(tenant_id, source, external_id, content_sha)` | `UNIQUE` — the idempotency / audit key |
| `record_type` | `commit` \| `pull_request` \| `issue` \| `message` \| … |
| `payload` / `body_ref` | small inline payload, or an S3 pointer for large bodies |

`ingest_repo.record_raw_event` returns `is_new`. **If a record's `(source, external_id, content_sha)` was already landed, the record short-circuits** — no re-normalize, no re-embed, no re-spend (`stats.skipped_duplicate += 1`). This makes `incremental_sync` cheap and correct: the GitHub connector implements incremental as a bounded full re-pull, and the landing dedup skips unchanged objects.

---

## 3. Normalize → graph upsert + identity resolution

Step 2 runs `ingestion/normalizer.upsert_graph(conn, record)` inside the **same tenant transaction** as landing, and returns the `NodeRef → entity_id` map the docs need later.

### Bitemporal entity upsert

`graph_repo.upsert_entity` writes the **current** entity for `(kind, natural_key)`. The conflict target is the partial unique index:

```sql
uq_entities_natural_current ON cypherx_a1.entities (tenant_id, kind, natural_key) WHERE valid_to IS NULL
```

so re-ingesting the same node **updates in place and keeps the same `entity_id`** (edges + citations stay valid). `attrs` is merged (`attrs || EXCLUDED.attrs`), `external_id` is `COALESCE`d. `tenant_id` comes from the RLS GUC (`NULLIF(current_setting('app.tenant_id', true), '')::uuid`), never the body. The `fts` column is a stored generated `tsvector` over `title || search_text`.

`entities` is **bitemporal**: `valid_from` defaults to `NOW()`, `valid_to IS NULL` denotes the current slice, and older versions retain a set `valid_to`. The `entities_kind_enum` CHECK (widened in Phase B) constrains `kind`.

### Identity resolution (exact handle/email match)

For a `person` node, the normalizer first calls `_resolve_person_by_handle`: if **any** of the node's `identity_handles` already maps to a canonical `person_entity_id` in `cypherx_a1.identities`, that existing entity is reused so **one human is not split across tools**. The handles are then recorded via `_record_identities` (`ON CONFLICT (tenant_id, source, handle) DO NOTHING`). The GitHub `_person` builder keys a person by **canonical lowercased email** and registers both a `github` login handle and an `email` handle — the cross-tool anchor.

> Today's identity resolution is **exact match only** (handle/email). Semantic identity resolution + a human-review queue is a Phase C forward plan, layered on top of this exact match (never replacing it).

### Edge upsert (deterministic ingest edges)

`upsert_graph` then wires the edges. `_resolve_ref` resolves each `NodeRef` to an `entity_id`, **stub-creating** a minimal entity (`source='derived'`) for any ref that names a node not present in this record — so an edge can always be created. Each edge is written with `graph_repo.upsert_edge` at `extractor_version='ingest'`, which **supersedes-in-place the current `(src, dst, rel)` edge** (UPDATE the row with `valid_to IS NULL` if present, else INSERT) — deterministic and idempotent for re-ingest. The `edges_rel_enum` CHECK (widened in Phase B to include `touched` / `summarizes`) constrains `rel`.

Hot-path indexes back the reads: `idx_edges_src`, `idx_edges_dst`, the current-slice `idx_edges_current`, and a dedicated `idx_edges_depends_on_current` keeping each recursive `impact_of` iteration index-only.

---

## 4. RAG ingest → vectors + citation link

Step 3 embeds each `RagDoc` into the SharedCore RAG corpus — an HTTP call **outside any DB transaction** — then records provenance in a second tenant transaction.

- **KB resolution.** `KbResolver` maps a logical KB name (e.g. `eng-code`) to a RAG `kb_id` **create-once per `(tenant, logical)`**, persisting the resolved embedding model + dim **immutably** into `cypherx_a1.rag_kbs` so every KB shares one stable vector space (the pinned-model guarantee). An in-process cache avoids a DB hit per doc.
- **Ingest + link.** `rag.ingest_inline(...)` is called with an `Idempotency-Key` of `f"{tenant_id}:{record.content_sha}:{doc.kb}"` (Contract 9). On return, `_link` records the `vector_ref = {kb_id, doc_id}` back onto the node (`graph_repo.set_vector_ref`), writes a `doc_id`-keyed row into `cypherx_a1.citations` (`ingest_repo.add_citation`), and enqueues a `cypherx.cypherxa1.record.normalized` Contract-5 event via the **outbox**.

The citation link is what makes hybrid retrieval reinforcing: a RAG chunk's `doc_id` maps back to its originating graph entity.

> **Webhook path is graph-only.** `/webhooks/{kind}` has no inbound agent JWT to forward to RAG, so when `rag`/`agent_jwt` are `None`, `_ingest_one` runs **landing + graph normalization only** and defers embedding to an authenticated sync/worker. Signatures are still verified.

The `outbox` table has **no RLS by design** — it is a cross-tenant publish queue drained by a background task that sets no `app.tenant_id`; isolation lives in the payload, not the row.

---

## 5. LLM extraction (Phase A: confidence floor + supersede chain) — BUILT

`src/cypherx_a1/extraction/extractor.py` enriches the graph with relationships raw ingest cannot see. It reads entities **not yet extracted at the current `extractor_version`** (default `1.0.0`) and asks the llms-gateway (`response_format=json_object`) for edges from the allowed vocabulary:

```
_EXTRACTABLE_RELS = {depends_on, decided_in, caused, resolved, expert_in, mentions}
_TARGET_KINDS     = {service, repo, feature, decision, incident, person, document, pr, ticket}
```

`owns` / `authored` / `reviewed` / `part_of` come from **deterministic ingest, not the LLM** — the LLM **never self-edits the deterministic spine of the shared graph**.

### Discipline: idempotency + cost (Contract 19)

Extraction is keyed in `cypherx_a1.extraction_jobs` by `(tenant_id, node_id, content_sha, extractor_version)` — re-ingest never re-spends. Each gateway call carries an `Idempotency-Key` (`_idem_key`); the gateway's `llm_call_id` + `cost_usd` are recorded verbatim (cypherx-a1 **never rewrites** the gateway's cost numbers). In keyless/mock-provider mode the gateway returns no useful JSON, so extraction yields few/no edges and simply records the job — the explicit ingest edges already answer the demo queries; extraction is strict enrichment.

### Phase A — confidence floor (`extractor._parse_edges`)

Every extracted edge is parsed, validated against the vocabulary, and clamped to `confidence ∈ [0,1]`. The **single confidence floor** (`extraction_confidence_floor = 0.6`) is then applied per `confidence_floor_mode`:

| `confidence_floor_mode` | Behaviour for `confidence < floor` |
| --- | --- |
| `flag` (default) | edge **kept** but marked `flagged=True` → written to `metadata.flagged=true` (preserves recall; readers can filter) |
| `drop` | edge **dropped** |

There is **one** floor and **one** mode — no per-relation tiering (deliberate; MemGPT-style tiering was explicitly *not* adopted).

### Phase A — explicit supersede chain (`graph_repo.upsert_extracted_edge`)

Before writing, `_extract_node` calls `supersede_extracted_edges` to bitemporally close prior extracted edges from the node (those with `extractor_version <> 'ingest'` and `<>` the new version). Then each edge is written via **`upsert_extracted_edge`**, the Phase A bitemporal upsert with an explicit supersede **link**:

1. Look up the current `(src, dst, rel)` edge.
2. If it exists and the content (confidence/metadata) is **materially unchanged**, leave it (return its `edge_id`).
3. If it **changed**, `UPDATE ... SET valid_to = NOW()` to close it, then INSERT a new edge whose **`supersedes_edge_id`** points at the closed one.
4. If none exists, INSERT fresh.

The new additive column `edges.supersedes_edge_id` (migration `20260614_0003__phaseA.sql`, indexed by `idx_edges_supersedes`) yields an **auditable contradiction chain** (new → old) rather than an unlinked `valid_to` close — grounded in Zep/Graphiti bi-temporal invalidation. **Verified live:** on a content change, the new edge links to the old and the old is closed.

---

## 6. Hybrid RRF retrieval (Phase A: graph-aware rerank) — BUILT

`src/cypherx_a1/retrieval/orchestrator.py` (`RetrievalOrchestrator.retrieve`) fuses **three independent legs** into a token-bounded, fully-cited context. Hybrid retrieval is **app-side** (RAG ships dense-only first cycle, so cypherx-a1 owns keyword + fusion + rerank):

| Leg | Source | Function |
| --- | --- | --- |
| **graph** | FTS/keyword + natural-key match over app-owned entities | `graph_repo.find_entities` |
| **keyword** | a second tsvector pass (the BM25-ish leg) | `graph_repo.keyword_search` |
| **rag-dense** | dense vector search across the per-tenant KBs (queried **concurrently**, fan-out) | `RagClient.query` |

Legs 1 + 3 (graph + keyword) and the KB-id list run in one tenant transaction; the RAG dense leg fans out over all KBs concurrently outside any tx (a forbidden KB or a per-KB transport error is skipped — the others still contribute).

### Fusion: reciprocal-rank fusion (RRF)

Each leg contributes `1.0 / (k + rank)` to an item's score, with **`retrieval_rrf_k = 60`**. A RAG hit's `doc_id` is mapped back to its graph entity (`ingest_repo.entities_for_docs`); when a chunk matches an entity the two **reinforce each other** — the entity item keeps `kind='entity'` (provenance is not relabelled) but gains the chunk's text + `doc_id`/`chunk_id` so the citation carries the real evidence. Unmatched chunks become standalone `chunk` items.

### Phase A — graph-aware rerank (`rerank_multiplier`)

`graph_repo.find_entities` / `keyword_search` now surface, per entity, its **strongest CURRENT edge confidence** (a correlated subquery — `max(confidence) WHERE valid_to IS NULL`, default `1.0`) and `created_at`. The orchestrator scales each fused RRF score by a pure, unit-testable factor:

```
multiplier = (1 + w_conf * confidence) * ((1 - w_rec) + w_rec * recency)
recency    = 0.5 ** (age_days / halflife)
```

| Config key | Default | Effect |
| --- | --- | --- |
| `rerank_confidence_weight` (`w_conf`) | `1.0` | high-confidence current edges outrank speculative ones |
| `rerank_recency_weight` (`w_rec`) | `0.5` | weight of the time term; `0` disables recency |
| `rerank_recency_halflife_days` | `90` | half-life of the recency decay |

Chunks (no edge) default to `confidence=1.0` / no recency. This adapts MemGPT precedence into the **rerank only** (not into tiering). The top `retrieval_context_max_chunks` items survive, and **every surviving item becomes a `Citation` — answers are never uncited.**

---

## 7. The query surfaces over the built KB

Three surfaces consume the graph + retrieval, all RLS-scoped via `in_tenant`:

### 7a. Cited copilot

`copilot/service` runs the cited flow: it calls the retrieval orchestrator, then llms-gateway and guardrails directly. Guardrails are **fail-closed** (`decision=block` → 422 `GUARDRAIL_VIOLATION`); Memory is best-effort (never fails an answer). Exposed at `POST /v1/copilot/ask`.

### 7b. Deterministic graph-query surface (`copilot/queries.py`)

`GraphQueryService` answers the core engineering-memory questions **with no LLM call**, returning structured items + `Citation` provenance. These back both the `/v1/graph/*` REST endpoints and the stateless `mcp-eng-memory` MCP server, so an autonomous coding agent gets fast, deterministic, source-cited answers:

| Method | Backed by | Answers |
| --- | --- | --- |
| `who_owns` | `graph_repo.owners_of` (rels `owns/authored/reviewed/expert_in`) | who owns a repo/service/feature |
| `what_breaks_if_changed` | `graph_repo.impact_of` (recursive reverse-`depends_on` blast radius) | transitive impact + owners, up to `max_hops` |
| `experts_on` | `graph_repo.experts_on` (FTS topic nodes × contribution edges) | strongest contributors on a topic |
| `why_built` | `graph_repo.find_entities` (pr/feature/decision/ticket/document) | the artifacts behind a feature |
| `neighbors` | `graph_repo.neighbors` (one-hop typed, `direction='both'`) | the local neighbourhood |
| `activity` | `graph_repo.activity_timeline` | the Phase B timeline (below) |

### 7c. Activity / change timeline (Phase B) — BUILT

`graph_repo.activity_timeline(scope_entity_id, since, until)` returns **current `change`/`pr`/`ticket`/`incident` nodes connected to a repo or person, newest first**, each resolving its `authored` author and an `occurred_at` (`attrs.timestamp` or `valid_from`). It is backed by the Phase B activity index:

```sql
idx_entities_activity ON cypherx_a1.entities (tenant_id, valid_from DESC)
  WHERE valid_to IS NULL AND kind IN ('change','pr','ticket','incident')
```

Exposed at `POST /v1/graph/activity` (`copilot/queries.activity`) and the MCP `what_changed` tool. **Verified live:** a cited, time-ordered "who did what, when".

---

## 8. Reflection / consolidation (Phase B) — BUILT

`src/cypherx_a1/extraction/consolidator.py` (`run_consolidation`) turns the accreting graph into **active knowledge** (the Generative-Agents reflection win, Park et al. 2023). It clusters each person's current contribution edges and synthesizes a short **expertise summary**.

1. **Cluster** — `graph_repo.consolidation_clusters` groups each person's current `authored` / `reviewed` / `owns` / `expert_in` edges with their target titles + ids and `avg(confidence)`, keeping clusters with `count >= consolidation_min_cluster` (`3`).
2. **Threshold** — a cluster is consolidated only if `avg_conf >= consolidation_avg_confidence` (`0.75`). One threshold, one floor (deliberate).
3. **Synthesize** — `_synthesize` asks the gateway (`json_object`) for a `{summary, topics}`; on any failure (keyless/mock) it writes a **deterministic fallback** so the pass still produces a node offline.
4. **Write** — an `expertise_summary` entity (`natural_key = f"expertise:{person_key}"`, `source='consolidation'`) plus `summarizes` edges to the subject person (`role=subject`) and each evidence artifact (`role=evidence`).

Discipline mirrors the extractor: **idempotent + cost-metered** via `extraction_jobs` keyed on the summary node + a `content_sha` of the cluster + `consolidation_version`; an unchanged cluster is skipped (no re-spend), a changed one **supersedes in place** (the entity keeps its id). Critically, the summary is **GRAPH-ONLY — never embedded into RAG** (honours the invariant).

**Triggers (both):** `POST /v1/extract?consolidate=true` and a scheduled worker tick (`worker/runner.py`, gated by `consolidation_schedule_enabled`, default `False`; it enumerates tenants from the non-RLS outbox). **Verified live:** 2 summaries written; an idempotent re-run → 0.

---

## 9. What is built vs. planned

| Phase | Status | Delivers |
| --- | --- | --- |
| **A** | **IMPLEMENTED + verified** | `edges.supersedes_edge_id` contradiction chain; `extraction_confidence_floor` + `confidence_floor_mode`; graph-aware RRF rerank (`rerank_*`) |
| **B** | **IMPLEMENTED + verified** | widened kind/rel CHECKs (`change`/`capability`/`expertise_summary`, `touched`/`summarizes`); activity timeline (`activity_timeline`, `/v1/graph/activity`, MCP `what_changed`); consolidation pass (`expertise_summary` nodes) |
| **C** | documented, **not** implemented | recency-decayed Degree-of-Knowledge `expert_in` + ownership-concentration metric; a regex query-type classifier → per-leg RRF weights; semantic identity resolution + human-review queue (atop exact match) |
| **D** | documented, **evidence-gated** | SZZ change→cause attribution into `caused` edges; single-hop Personalized-PageRank reviewer recommendation; RAPTOR-L2 meta-consolidation for >1k-edge tenants — **build ONLY on a measured trigger** |

### Surgical-by-design guardrails (do not breach when extending)

A 5-agent research pass over 38 works concluded the design is already at 2024–2025 SOTA for temporal-graph agent memory, so wins are **surgical**:

- No new DB / service / graph-engine swap; no Apache AGE / Neo4j; no community detection (Leiden/GraphRAG); no Code Property Graphs.
- No PPR-multihop or RAPTOR-L2 before a measured need; no LLM-judge reranking; no RRF parameter sweeps.
- **One** confidence floor + **one** consolidation threshold.
- The **LLM never self-edits the shared graph's deterministic spine**; consolidation summaries are graph-only and never pushed into SharedCore.
- Only **additive** `/v1` + MCP changes — no published contract broken.
