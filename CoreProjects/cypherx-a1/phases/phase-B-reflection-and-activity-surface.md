# Phase B — active memory + the change/activity surface (IMPLEMENTED)

> Two surgical wins, built and verified live against real Postgres + over HTTP + MCP: a **change/activity surface** that answers "who did what, when" as a cited time-ordered timeline, and a **reflection/consolidation pass** (Generative-Agents style) that turns the accreting graph into *active knowledge* — `expertise_summary` nodes synthesized from each person's contribution clusters, idempotent + cost-metered, graph-only, never embedded into RAG.

## Where this sits

cypherx-a1 is already at 2024–2025 SOTA for temporal-graph agent memory (a 5-agent research pass over 38 works confirmed it), so Phase B adds **no new DB, service, engine, or contract** — only additive SQL (two widened `CHECK` enumerations + one index), additive `/v1` endpoints, and one new MCP tool. Phase A delivered the bitemporal precedence machinery (`edges.supersedes_edge_id`, confidence floor, confidence×recency rerank); Phase B builds the *product-visible* surface on top of it. Phase C and Phase D remain forward plans (see [Forward plans](#forward-plans-phase-c--d)).

| Research grounding | Adopted as |
| --- | --- |
| Generative Agents reflection (Park et al. 2023) | the reflection/consolidation pass → `expertise_summary` nodes |
| MSR issue↔commit linking | the activity/change surface + the time-ordered `activity_timeline` |

Both are **ADOPT** verdicts. Everything heavier (RAPTOR L2, A-MEM reinforcement, HippoRAG PPR multi-hop, SZZ defect attribution, cross-encoder reranking, GraphRAG community-detection/Leiden, CPGs, a graph-engine migration) is explicitly **DEFER** or **SKIP** and does not appear here.

---

## Part 1 — the change/activity surface

### Schema (additive)

`db/migrations/20260614_0004__phaseB.sql` widens two `CHECK` enumerations and adds one partial index. Existing rows stay valid; no RLS change.

| Object | Change |
| --- | --- |
| `cypherx_a1.entities.kind` (`entities_kind_enum`) | `+ 'change'` (a discrete change event — a commit, or a PR/ticket-transition when commit granularity is unavailable); `+ 'capability'`, `+ 'expertise_summary'` (reflection summaries) |
| `cypherx_a1.edges.rel` (`edges_rel_enum`) | `+ 'touched'` (change → repo/file it changed); `+ 'summarizes'` (a summary entity → the evidence it was synthesized from) |
| `idx_entities_activity` | partial index `ON entities (tenant_id, valid_from DESC) WHERE valid_to IS NULL AND kind IN ('change','pr','ticket','incident')` — backs the timeline read |

### Commit-level `change` nodes + configurable granularity

`connectors/github.py` emits commit-level change events. `_change_record(...)` builds, for each commit, a `change` node keyed `"{full_name}@{sha}"` (carrying `attrs.sha`, `.message`, `.timestamp`, `.repo`, `.author`, `.files`, `.url` → the GitHub commit URL) plus two edges:

- `authored`: `person → change` (with `metadata.ts`)
- `touched`: `change → repo` (with `metadata.files`)

Granularity is config-driven via `connector_change_granularity` (`core/config.py`, default `"auto"`):

| `connector_change_granularity` | Behaviour |
| --- | --- |
| `auto` | commit-level `change` nodes where the commit stream is available, else PR/ticket transitions |
| `commit` | force per-commit `change` nodes |
| `pr_ticket` | PR + ticket-transition only (no per-commit nodes) |

`_fixture_records(granularity)` emits the two demo commits (`c0ffee1` by Alice, `beef002` by Bob) only when granularity is `auto` or `commit`, so the keyless fixture path exercises the full timeline offline.

### The timeline query

`db/graph_repo.activity_timeline(conn, *, scope_entity_id, since=None, until=None, limit=50)` returns the **current** `change`/`pr`/`ticket`/`incident` nodes connected to a scope entity (a repo *or* a person), newest first. Each row resolves an `occurred_at` (`COALESCE((attrs->>'timestamp')::timestamptz, valid_from)`) and an `author` (+ `author_key`) via a correlated lookup of the node's current `authored` edge from a current `person`. `DISTINCT ON (entity_id)` de-dupes; optional `since`/`until` bound `occurred_at`. Only `valid_to IS NULL` rows participate, so the timeline reflects current truth, not superseded history.

### Endpoint + copilot wiring

| Layer | Detail |
| --- | --- |
| HTTP | `POST /v1/graph/activity` → `api/graph.activity` (scope `graph:activity`). Body `ActivityRequest { target (repo `owner/name` or person), since?, until? }` (`extra="forbid"`). Returns a `GraphAnswer { items, citations, trace_id }`. |
| Copilot | `copilot/queries.GraphQueries.activity(...)` resolves `target` → entity, calls `activity_timeline`, and shapes each row into `{ activity, kind, natural_key, author, when, via, url }`. |
| Citations | the resolved scope entity **plus every timeline row** are emitted as `Citation`s — the answer is cited "who did what when", not a bare list. |

### MCP tool `what_changed`

`mcp-eng-memory/manifest.json` declares `what_changed` — *"Time-ordered 'what changed, who worked on it, when' for a repo or person, with source citations."* (`idempotent: true`, `timeout_seconds: 25`). Input schema (`additionalProperties: false`): `target` (required, `minLength 1`), `since?`, `until?` (ISO-8601, `maxLength 40`). `src/mcp_eng_memory/api/invoke.py` dispatches it to the backend `POST /v1/graph/activity`, forwarding `since`/`until` when present and returning `{ items, citations }`. The MCP facade stays **stateless** — no DB, no metering of its own; per-invocation metering belongs to the calling xAgent's outbox.

---

## Part 2 — the reflection / consolidation pass

### What it does

`extraction/consolidator.run_consolidation(pool, *, tenant_id, agent_jwt, agent_id, llms, settings)` turns the graph into *active knowledge*. For each person it clusters their current contribution edges, and for high-confidence clusters synthesizes a short expertise summary into a new `expertise_summary` node with `summarizes` evidence edges. It mirrors the extractor's discipline exactly.

### Clustering

`db/graph_repo.consolidation_clusters(conn, *, rels, min_cluster, limit)` returns, per current `person`, the cluster of their current edges over `rels = ('authored','reviewed','owns','expert_in')` (`_CLUSTER_RELS`) joined to current target entities: `cnt`, `avg_conf` (`avg(e.confidence)`), and the first 8 `target_titles` + `target_ids`. The `HAVING count(*) >= min_cluster` prunes thin clusters in SQL. Only `valid_to IS NULL` rows on both edges and entities participate.

### Thresholds (exactly one floor, exactly one cluster size)

| Config key (`core/config.py`) | Default | Role |
| --- | --- | --- |
| `consolidation_avg_confidence` | `0.75` | min cluster average confidence to summarize (else `skipped`) |
| `consolidation_min_cluster` | `3` | min edges in a cluster before it is considered |
| `consolidation_version` | `"1.0.0"` | part of the idempotency `content_sha`; bump forces re-consolidation |
| `consolidation_max_tokens` | `512` | synthesis cap |
| `consolidation_lookback_limit` | `500` | recent edges scanned per run |
| `consolidation_schedule_enabled` | `false` | enables the scheduled worker tick |
| `consolidation_interval_seconds` | `86400` | tick period (daily) |

This honours the guardrail of **one confidence floor + one consolidation threshold** — no per-relation knobs, no tiering, no parameter sweeps.

### Synthesis + invariants

`_consolidate_one(...)` computes the cluster `content_sha = sha256("{sorted target_ids}:{version}")` and the summary natural key `f"expertise:{person_key}"`, then:

1. **Idempotency / cost-metering** — checks `ingest_repo.extraction_job_done(node_id, content_sha, extractor_version)`. If this exact cluster was already consolidated at this version, it returns `False` (counted `skipped`) and **spends nothing**. The same `extraction_jobs` ledger the extractor uses gates the spend.
2. **Synthesis** — `_synthesize(...)` calls the **llms-gateway** (`response_format={"type":"json_object"}`, `temperature 0.2`, `idempotency_key="consolidate:{content_sha}"`) for `{summary, topics}`. If the gateway returns no usable JSON (keyless/mock), a **deterministic fallback** summary is written so the pass still produces a node offline.
3. **Write (graph-only)** — `graph_repo.upsert_entity(kind="expertise_summary", source="consolidation", natural_key=…, …)` then `summarizes` edges: one to the subject `person` (`metadata.role="subject"`) and one to each evidence target (`metadata.role="evidence"`). The summary is **NEVER embedded into RAG** — it carries no `vector_ref`, honouring the crown-jewel invariant (the graph never enters RAG or Memory).
4. **Supersede-on-rerun** — because the summary keeps the same `natural_key`, a *changed* cluster supersedes the prior summary in place (same entity id; edges supersede-in-place via Phase A's `upsert_edge`/`supersedes_edge_id` machinery), never duplicating. An *unchanged* cluster is skipped.
5. **Ledger** — `ingest_repo.record_extraction_job(node_id, content_sha, extractor_version, edges_extracted, llm_call_id, cost_usd)` records the spend; `metrics.extraction_jobs_total{result}` increments.

Per-person failures are caught (`stats.failed`) and never abort the pass. The LLM **never self-edits the shared graph** — it only proposes `{summary, topics}` text; the app writes the nodes/edges deterministically.

### Both triggers (the "both" decision)

| Trigger | Path |
| --- | --- |
| On-demand (primary, agent-scoped) | `POST /v1/extract?consolidate=true` → `api/connectors.extract` runs `run_extraction` then `run_consolidation`; the `ExtractResponse` adds `summaries_written` + `persons_consolidated`. |
| Scheduled (background) | `worker/runner.py` — when `consolidation_schedule_enabled`, `_consolidation_tick` enumerates active tenants via `SELECT DISTINCT partition_key FROM cypherx_a1.outbox` (the **non-RLS** outbox, scannable cross-tenant) and runs `run_consolidation` per tenant every `consolidation_interval_seconds`. The Kafka ingestion consumer remains a documented seam (Phase 1.5). |

---

## Live-verification evidence

All verified against real Postgres, over HTTP, and through MCP (per the project status: *"verified live against real Postgres + over HTTP + MCP"*).

| Check | Evidence |
| --- | --- |
| Activity timeline | `POST /v1/graph/activity` for `acme/payments` returns a cited, time-ordered "who did what when" — the `change`, `pr`, and `ticket` nodes newest-first, each attributed to its author with the commit/PR URL as a citation. |
| Consolidation produces summaries | a first run wrote **2** `expertise_summary` nodes from the high-confidence contribution clusters (`avg_conf >= 0.75`, `count >= 3`). |
| Consolidation is idempotent | an immediate re-run wrote **0** (every cluster's `content_sha` already in `extraction_jobs` → `skipped`, no LLM spend). |
| MCP chain | the `what_changed` MCP tool dispatches to backend `/v1/graph/activity` and returns the cited timeline, forwarding `since`/`until`. |
| Graph-only invariant | the summary node carries no RAG vector; nothing is pushed into SharedCore. |

---

## Over-engineering avoided

Held to the Phase-B guardrails:

- **No new DB / service / graph-engine swap.** Additive SQL only (two widened `CHECK`s + one partial index); still the frozen `pgvector/pg16` adjacency-list graph; `cxa1_user` never needs `CREATE EXTENSION`.
- **No community detection, Leiden, CPGs, or PPR-multi-hop / RAPTOR-L2.** Consolidation is a flat per-person clustering, not hierarchical community summarization.
- **No LLM-judge reranking, no RRF parameter sweeps.** Phase B touches neither retrieval fusion nor reranking (those are Phase A and stay fixed).
- **Exactly one confidence floor + one consolidation threshold.** No per-relation tiering or knobs.
- **The LLM never self-edits the shared graph.** It proposes summary text; the app writes nodes/edges deterministically, with a keyless deterministic fallback.
- **Only additive `/v1` + MCP.** `POST /v1/graph/activity`, `?consolidate=true`, and the `what_changed` tool are all additive — no contract broken.

---

## Forward plans (Phase C & D)

Documented, **not** implemented — included only so the surgical scope of Phase B is clear. Build Phase D items **only on a measured trigger**.

| Phase | Plan | Status |
| --- | --- | --- |
| C | Recency-decayed Degree-of-Knowledge `expert_in` + ownership-concentration metric; a lightweight regex query-type classifier → per-leg RRF weights; semantic identity resolution + human-review queue (on top of exact handle/email match). | documented, not yet implemented |
| D (evidence-gated) | SZZ change→cause attribution into `caused` edges; single-hop Personalized-PageRank reviewer recommendation; RAPTOR L2 meta-consolidation for >1k-edge tenants. | documented; build ONLY on a measured trigger |
