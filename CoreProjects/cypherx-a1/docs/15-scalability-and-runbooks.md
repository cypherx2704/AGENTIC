# Scalability & runbooks

> How cypherx-a1 scales an engineering-memory graph + RAG corpus and how to operate it under load and incidents: backfill throttling vs the llms/RAG embedding limits it consumes, graph cardinality ceilings, transitive-closure precompute for hot `what_breaks`, incremental change detection (`content_sha`), the re-ingest cost-vs-correctness tradeoff (RAG has **no in-place update**), and three operational runbooks (a connector backfill, an embedding-path outage, a tenant offboarding / GDPR wipe) — written from the code in `src/cypherx_a1/ingestion/pipeline.py`, `db/graph_repo.py`, `db/ingest_repo.py`, `extraction/extractor.py`, `api/connectors.py`, `api/webhooks.py`, `services/rag_client.py`, `services/memory_client.py`, `core/config.py`, and `db/migrations/20260614_0001__init.sql`.

---

## 0. Scope and mental model

cypherx-a1 has three distinct workloads, each with a different scaling profile and a different bottleneck:

| Workload | Driver | Bottleneck | Where the cost is | Cheap to retry? |
| --- | --- | --- | --- | --- |
| **Ingest / backfill** | `POST /v1/connectors/{kind}/sync` and `POST /webhooks/{kind}` | RAG inline-ingest HTTP + embeddings (indirect) | RAG embedding tokens (metered downstream) | Yes — `content_sha` dedup makes re-runs idempotent |
| **Extraction** | `POST /v1/extract` | llms-gateway chat calls (`json_object`) | gateway `cost_usd` per node | Yes — `extraction_jobs` ledger never re-spends |
| **Retrieval / copilot** | `POST /v1/copilot/ask`, `POST /v1/graph/*`, MCP tools | Postgres recursive-CTE traversal + RAG dense query | CPU on Postgres; one copilot gateway call | N/A (read path) |

The guiding invariant for all three: **cypherx-a1 owns the graph and the orchestration logic; SharedCore owns the embeddings, the vectors, and the provider calls.** Every scaling decision is therefore split between "what we can tune in our own loop" (page size, dedup, precompute, concurrency) and "what we must stay inside the contract for" (RAG `top_k`/`ef_search` clamps, llms idempotency, never rewriting the gateway's cost). We never push throttling or backpressure into SharedCore — we shape our own call rate.

---

## 1. Backfill throttling vs the llms / RAG embedding limits

### 1.1 The two clocks

A connector backfill has two independent rate limits in tension:

1. **The source's clock** — GitHub's REST/GraphQL secondary rate limits. Bounded by `backfill_page_size` (default `100`) per stream tick and resumable via `sync_cursors`.
2. **The downstream embedding clock** — every doc the backfill produces is sent to RAG `POST /v1/kbs/{kb_id}/documents` (`RagClient.ingest_inline`), and RAG synchronously chunks + **embeds** it via the llms embedding model. The embedding tokens are the real money and the real upstream rate limit.

`cypherx-a1` does **not** see the embedding limit directly — it reaches embeddings *indirectly* through RAG (the boundary doc forbids calling `/v1/embeddings` for the corpus). So the only lever we have is **how fast we hand documents to RAG**.

### 1.2 What the pipeline does today (and where the seam is)

The MVP pipeline is **sequential and synchronous** inside one `sync` request. Walk `ingest_records` → `_ingest_one` in `ingestion/pipeline.py`:

```
for record in records:                      # one record at a time
    land + normalize (one tenant tx)        # cheap, DB-only
    if duplicate (content_sha): skip        # no embed, no spend
    for doc in record.docs:
        kb_id = kb_resolver.resolve(...)     # cached after first hit
        rag.ingest_inline(...)               # ← the embedding HTTP call (outside any tx)
        link vector_ref + citation + event   # one tenant tx
```

There is **no concurrency and no rate limiter in the loop today** — records are processed one at a time, and the `rag.ingest_inline` call is `await`ed before the next doc. That is the throttle: the backfill is naturally paced by RAG's own latency. This is deliberate for the MVP (correctness over throughput) and is the documented scale-out seam — `worker/runner.py`.

The throttle knobs that **do** exist and bound the blast radius per request:

| Knob | Default | Where | Effect on the embedding clock |
| --- | --- | --- | --- |
| `backfill_page_size` | `100` | `core/config.py` | Caps records pulled per stream tick → caps docs embedded per `sync` call. Lower it to slow a hot backfill. |
| `SyncRequest.mode` | `incremental` | `api/connectors.py` | `incremental` honours the stored `sync_cursors`; only **new** pages are pulled → far fewer embeds than a `full` re-sweep. |
| `rag_timeout_seconds` | `30.0` | `core/config.py` | Bounds how long one inline ingest can block the loop before failing that record. |
| `content_sha` dedup | always on | `raw_events` unique index | A duplicate record **short-circuits before any RAG call** — re-runs cost zero embedding tokens. |

### 1.3 Per-document idempotency at RAG

Every `ingest_inline` call carries an `Idempotency-Key` of the form:

```
f"{tenant_id}:{record.content_sha}:{doc.kb}"
```

(see `pipeline.py:_ingest_one`). This means even if a backfill is killed mid-flight and re-run, RAG itself will **not** re-embed a doc whose `(tenant, content_sha, kb)` triple already landed — the embedding spend is bounded by *distinct content*, not by *number of attempts*. The app-side `raw_events` dedup is the first gate; the RAG `Idempotency-Key` is the second (defence in depth, because the webhook path lands a record without embedding, and a later authenticated sync embeds it).

### 1.4 RAG-side clamps we must respect

The query path (not the ingest path) is where RAG enforces hard ceilings, and `RagClient.query` clamps to them defensively so we never get a 422 for over-asking:

| Parameter | App default (`core/config.py`) | RAG hard cap | Clamp site |
| --- | --- | --- | --- |
| `top_k` | `rag_query_top_k = 20` | `100` | `min(top_k, 100)` in `RagClient.query` |
| `ef_search` | `rag_query_ef_search = 100` | `500` | `min(..., 500)` in `RagClient.query` |
| inline doc size | — | `≤ 100 KiB` | enforced upstream; we keep docs small per source |
| filters | `@>`-containment only | — | app does range/time filtering itself |

**Do not raise these past the caps.** A higher `ef_search` buys recall at super-linear CPU cost on the RAG side and there is a contractual ceiling; if recall is insufficient, fix it app-side with RRF fusion + keyword (`retrieval_*` knobs), not by hammering RAG.

### 1.5 Recommended throttling posture

- **Local / demo:** `CONNECTOR_MODE=mock`, `MOCK_EMBEDDINGS=true` upstream — backfill is keyless and free; page size irrelevant.
- **Live, first real org:** keep `backfill_page_size=100`, run `sync` with `mode=incremental` on a schedule, and let RAG latency pace the loop. Watch `cypherx_a1_downstream_calls_total{service="rag",result="..."}` and the gateway's own token/cost meters.
- **Live, large historical backfill:** do the **first** full sweep with `mode=full` during a quiet window, then switch to `incremental` forever. If RAG starts 429-ing, **lower `backfill_page_size`** (smaller batches per tick) rather than adding retries — the cursor makes the next tick resume exactly where this one stopped.
- **Never** parallelise the embed loop beyond what RAG's documented concurrency allows; the scale-out path is the Kafka worker (`worker/runner.py`) with `worker_max_attempts=3`, which gives per-record backpressure and a `.dlq`, not a fan-out that overruns the embedding budget.

---

## 2. Graph cardinality ceilings

The graph is an **adjacency list** (`cypherx_a1.entities` + `cypherx_a1.edges`) traversed by recursive CTEs. This is mandated (frozen `pgvector/pgvector:pg16` image, `cxa1_user` cannot `CREATE EXTENSION`, no Apache AGE/ltree) and the `GraphRetriever` seam exists so a later engine swap touches no SharedCore. That choice has concrete cardinality implications.

### 2.1 What grows, and how fast

| Table | Growth driver | Practical ceiling on Postgres-CTE | Mitigation |
| --- | --- | --- | --- |
| `entities` | one current row per `(tenant, kind, natural_key)` + bitemporal history | millions of current rows per tenant fine (B-tree + GIN) | partial unique index on the **current slice** keeps "current" reads sharp |
| `edges` | one current row per `(src, dst, rel)` + history | hot spot for traversal — **fan-out × hops** is the killer | bounded hops + `LIMIT` on every read; precompute (§3) |
| `raw_events` | one row per distinct `content_sha` (immutable landing) | unbounded but append-only; never traversed | retention/archival job (future); not on any hot path |
| `citations` | one row per `(kb_id, doc_id, entity)` link | grows with docs ingested | indexed by `(tenant, chunk_id)`; read by exact key only |
| `outbox` | one row per event; drained + (optionally) reaped | bounded by drain rate | partial index `WHERE published_at IS NULL`; reap published rows |

### 2.2 The traversal ceiling is fan-out, not node count

Recursive-CTE blast-radius queries (`impact_of`, the `experts_on` topic-node CTE) are bounded **three ways in code**, and every one of them is load-bearing:

1. **Hop cap.** `impact_of(..., max_hops)` stops the recursion at `b.depth < %(max_hops)s`. The copilot/retrieval default is `retrieval_max_hops = 3`. A `depends_on` graph with average out-degree *d* visits up to *O(dᵏ)* edges at *k* hops — at *d=10, k=5* that is 100 000 edge visits per query. **Three hops is the deliberate ceiling**; raising `max_hops` past ~4 on a dense dependency graph is the single fastest way to make `what_breaks` slow.
2. **Result `LIMIT`.** Every read function takes a `limit` (`impact_of` default `50`, `neighbors` `25`, `owners_of`/`experts_on` `10`, `find_entities` `20`). These cap the *output*, not the *work*, so they protect the copilot's context budget, not Postgres CPU — the hop cap is what protects CPU.
3. **Current-slice filter.** Every traversal predicate carries `valid_to IS NULL`, hitting the partial index `idx_edges_current (tenant_id, src_entity_id, rel) WHERE valid_to IS NULL`. Bitemporal history rows are invisible to the hot path, so history growth does **not** slow current traversal.

### 2.3 The index inventory the traversal depends on

From the init migration — do not drop these; they are the cardinality safety net:

| Index | Serves |
| --- | --- |
| `uq_entities_natural_current (tenant_id, kind, natural_key) WHERE valid_to IS NULL` | upsert dedup + stable `entity_id` across re-ingest |
| `idx_edges_src (tenant_id, src_entity_id, rel)` | forward traversal, `neighbors(direction=out)` |
| `idx_edges_dst (tenant_id, dst_entity_id, rel)` | **reverse** traversal — `impact_of`, `owners_of`, `experts_on` all start from `dst` |
| `idx_edges_current (tenant_id, src_entity_id, rel) WHERE valid_to IS NULL` | current-only recursion |
| `idx_entities_fts (GIN on fts)` | keyword leg (`find_entities`, `keyword_search`, `experts_on` topic CTE) |

> **Cardinality red flags to watch:** a single `service`/`repo` node with thousands of inbound `depends_on` edges (a "god node") will blow up `impact_of` fan-out at depth 1. The `experts_on` topic CTE already caps its seed set at `LIMIT 200` topic nodes for exactly this reason. If a tenant has god nodes, prefer the **precompute** in §3 over raising the hop cap.

---

## 3. Transitive-closure precompute for hot `what_breaks`

### 3.1 The problem

`what_breaks(X)` is the product's signature query and the one most likely to be **hot** (asked repeatedly by coding agents over MCP before every risky change) and **expensive** (reverse-`depends_on` recursion, the deepest traversal we run). Today it executes `graph_repo.impact_of` — a live recursive CTE — on every call. That is correct and cheap on a sparse graph, but it is *O(fan-outᵏ)* and recomputed from scratch each time, which does not amortise across the many agents asking the same question about the same hot service.

### 3.2 The precompute design (seam, not yet built)

Materialise the reverse-`depends_on` **transitive closure** for the current edge slice into a derived table, refreshed when the dependency topology changes:

```sql
-- derived, app-owned, tenant-scoped, RLS like every other table
-- cypherx_a1.impact_closure (tenant_id, target_entity_id, dependent_entity_id, min_depth, computed_at)
-- PRIMARY KEY (tenant_id, target_entity_id, dependent_entity_id)
```

- **Population** is the existing recursive CTE (`impact_of`) run once per current target, writing every `(target, dependent, min_depth)` pair. `what_breaks(X)` then degrades to an indexed point lookup `WHERE target_entity_id = X` — no recursion, *O(result-size)*, trivially cacheable.
- **Correctness window.** The closure is a **cache of the current slice**, so it is only as fresh as its last refresh. Acceptable because the dependency graph changes far more slowly than it is queried (it moves on PR merges, not on every commit).

### 3.3 Incremental invalidation keyed off ingestion

The refresh trigger is already produced: every normalized record emits `cypherx.cypherxa1.record.normalized` via the outbox, and extraction supersedes `depends_on` edges (`supersede_extracted_edges`) under a new `extractor_version`. So the closure invalidation rule is:

> Recompute the closure for any `target` whose **inbound `depends_on` set changed** since `computed_at` — i.e. an edge with `rel='depends_on'` was inserted (new) or had `valid_to` set (superseded) touching the target's reverse-reachable set.

The Kafka worker (`worker/runner.py`, consumer group `cypherx-cypherxa1-workers`) is the natural home: consume `record.normalized`, mark affected targets dirty, and recompute dirty closures in the background. Until that lands, a **scheduled full recompute** (one `impact_of` sweep per current node) during a quiet window is the stopgap, identical in spirit to the extraction-pass scheduling.

### 3.4 When precompute is worth it

| Signal | Implication |
| --- | --- |
| `what_breaks`/`impact_of` is in the p99 latency tail | precompute pays off |
| dense `depends_on` graph (god nodes, deep chains) | precompute pays off **a lot** (turns *O(dᵏ)* into a lookup) |
| sparse graph, low query rate | **do not** precompute — the live CTE is already sub-ms and the closure table is pure overhead + staleness risk |

Keep `impact_of` as the source of truth and the closure as a derived cache; never let the two disagree silently — stamp `computed_at` and expose closure staleness as a metric.

---

## 4. Incremental change detection (`content_sha`)

`content_sha` is the spine of every cost-control mechanism in the system. It is a hash of a record's canonical content, computed by the connector, and it is the third column of the `raw_events` idempotency key.

### 4.1 The three gates `content_sha` powers

| Gate | Location | What it prevents |
| --- | --- | --- |
| **Landing dedup** | `ingest_repo.record_raw_event` → `ON CONFLICT (tenant_id, source, external_id, content_sha) DO NOTHING` | re-landing + re-normalizing + re-embedding an unchanged record |
| **RAG embed dedup** | `Idempotency-Key = "{tenant}:{content_sha}:{kb}"` on `ingest_inline` | re-embedding the same content even if landing dedup is bypassed |
| **Extraction dedup** | `extraction_jobs` PK `(tenant_id, node_id, content_sha, extractor_version)` + `extraction_job_done` precheck | re-spending an LLM call on a node whose content has not changed |

### 4.2 The flow (`_ingest_one`)

```
record_raw_event(content_sha) → is_new?
  ├─ False  → stats.skipped_duplicate++  → RETURN   (no graph write, no embed, no spend)
  └─ True   → upsert_graph(...)          → embed each doc → citation + event
```

A duplicate record short-circuits **before** `upsert_graph`, before any RAG call, before any outbox event. This is why a backfill is safe to re-run blind: only *changed* content does any work. `IngestStats.skipped_duplicate` is the observable proof — a re-run of an unchanged backfill should report `records_seen = N, records_new = 0, skipped_duplicate = N, docs_ingested = 0`.

### 4.3 Change is content-addressed, not timestamp-addressed

A record is "new" iff its `content_sha` differs from what landed. Consequences:

- **An edit that changes the body** → new `content_sha` → new landing → `upsert_entity` updates the current entity **in place** (same `entity_id`, via the partial-unique conflict target) so edges and citations stay valid → a new RAG doc is embedded (RAG cannot update in place — see §5) → extraction re-runs because the `(node_id, content_sha)` pair is new.
- **A no-op re-sync** (same content) → fully deduped at all three gates → zero cost.
- **A model/prompt bump** (`extractor_version` change) → content_sha is unchanged but the extraction key's `extractor_version` component changes → extraction re-runs and **supersedes** prior extracted edges bitemporally, *without* re-landing or re-embedding (extraction reads the already-landed entity).

### 4.4 The "unextracted" worklist

`ingest_repo.list_unextracted_entities` is the incremental detector for the extraction pass: current entities of an extractable kind (`pr,ticket,incident,decision,document`) that have a `content_sha` but **no completed `extraction_jobs` row** at the current `extractor_version`. This is what makes `POST /v1/extract` resumable and idempotent — it only ever picks up genuinely-new or genuinely-changed work, bounded by `limit` (default `50` per call).

---

## 5. Re-ingest cost vs correctness (RAG has no in-place update)

### 5.1 The asymmetry

The graph and RAG handle a content change **differently**, and the difference is the crux of the cost/correctness tradeoff:

| Store | On a changed `content_sha` | In-place update? | Cost |
| --- | --- | --- | --- |
| **Graph** (`entities`) | `upsert_entity` → `ON CONFLICT ... DO UPDATE` on the current slice | **Yes** — same `entity_id`, `attrs` merged (`||`), edges/citations stay valid | one cheap DB write |
| **Graph** (`edges`) | `upsert_edge` supersede-in-place on the current `(src,dst,rel)`; extraction `supersede_extracted_edges` then re-extracts | **Yes** — bitemporal supersede, no dup | one DB write |
| **RAG** (`documents`) | a **new** `doc_id` is created by `ingest_inline`; the old doc is **not** mutated | **No** — RAG ships no in-place document update first cycle | a **new embedding spend** + an orphaned old doc/vector |

So: **the graph re-converges for free, but RAG accumulates.** Every meaningful edit to an already-embedded artifact creates a *fresh* RAG document and *fresh* embeddings; the previous vector for that artifact is still sitting in the KB.

### 5.2 Why we accept it (correctness wins, bounded by `content_sha`)

- **Idempotency caps the bleed.** The `Idempotency-Key = "{tenant}:{content_sha}:{kb}"` means an *unchanged* doc is never re-embedded. Only *genuine content changes* produce a new embedding — which is the minimum correct cost. We never pay twice for the same bytes.
- **Citations stay anchored to the entity, not the doc.** `citations` and `vector_ref` are re-pointed in the linking tx (`set_vector_ref` overwrites with the new `{kb_id, doc_id}`). The copilot maps a RAG hit back to its graph entity via `entities_for_docs` / `entities_for_chunks`, and the entity is stable. So a stale old vector that still happens to surface in a dense query resolves to the **same current entity** — wrong snippet, right node — and the graph leg (always current) dominates RRF fusion. Correctness of the *answer's provenance* is preserved.
- **The graph is the crown jewel; RAG is a recall aid.** Hybrid retrieval (graph + dense + keyword, RRF) means a stale RAG chunk degrades recall slightly, never correctness of ownership/impact answers, which come from the always-current graph.

### 5.3 The cost we must manage

Orphaned old documents/vectors are **dead weight**: they cost storage, marginally dilute dense recall, and accumulate per edit. Mitigations, in order of preference:

1. **Don't re-embed what didn't change** — already enforced by `content_sha` + `Idempotency-Key`. This is 90% of the win.
2. **Prefer coarse, stable doc granularity** — one `RagDoc` per graph node (the design), not per revision, so churny artifacts replace one doc rather than spraying many.
3. **Periodic KB compaction** (operational, when RAG exposes delete) — reconcile `cypherx_a1.citations`/`vector_ref` (the *current* `doc_id` per entity) against the KB and delete docs no entity points at. Until RAG ships document deletion in its `/v1` contract, this is a **documented future task**, not a live job — and we tolerate the orphans because they are bounded by edit volume, not by query volume.
4. **Never** "fix" staleness by re-embedding the whole corpus on a schedule — that re-spends embedding tokens on unchanged content and is exactly what `content_sha` exists to prevent.

> **Guard:** the embedding model is pinned per KB (`rag_kbs.embedding_model_resolved`, immutable, never the repointable `embed` alias). A re-embed under a *different* model would split the vector space and silently break dense recall. If the platform ever rotates the embedding model, that is a **new KB** (new `logical_name` → new `kb_id`) and a one-time deliberate re-ingest, not an in-place change.

---

## 6. Runbooks

All three runbooks assume the compose stack from the workspace `CLAUDE.md` (migrate job applied, `cypherx-a1` host `8093`, deps healthy). Identity on every authenticated call is the agent JWT (Contract 1) carried as `Authorization: Bearer`; cypherx-a1 mints the Contract-12 service token + forwards `X-Forwarded-Agent-JWT` to SharedCore itself.

### 6.1 Runbook A — Run (or recover) a connector backfill

**Goal:** ingest a GitHub repo's history into the graph + RAG, safely, idempotently, and without overrunning the embedding budget.

**Preconditions**
- `cypherx-a1` `/readyz` is `200` (Postgres reachable + Auth JWKS warm).
- For a live pull: `CONNECTOR_MODE=live` and `GITHUB_TOKEN` set; otherwise `CONNECTOR_MODE=mock` replays bundled fixtures (keyless).
- The caller's agent JWT carries an ingest scope (`require_scope(principal, ingest_scopes(), "connector:sync")`).

**Steps**

1. **Dry-run small.** Kick the first sync incrementally to validate auth + KB resolution before a big sweep:
   ```bash
   curl -sS -X POST http://localhost:8093/v1/connectors/github/sync \
     -H "Authorization: Bearer $AGENT_JWT" \
     -H "Content-Type: application/json" \
     -d '{"repo":"owner/name","mode":"incremental"}'
   ```
   Inspect the `SyncResponse`: `records_seen / records_new / nodes_upserted / edges_upserted / docs_ingested / skipped_duplicate / errors`.
2. **Full historical sweep (first time only), in a quiet window:** send `"mode":"full"`. This walks every `connector.streams()` from the start; `sync_cursors` is updated per stream as it goes, so the sweep is resumable.
3. **Watch the embedding clock.** Monitor `cypherx_a1_downstream_calls_total{service="rag"}` (labels `ok|rejected|forbidden|error`) and the gateway's own token/cost meters. If RAG starts rejecting (`rejected`/timeouts), **lower `backfill_page_size`** and re-issue — do not add retries.
4. **Switch to incremental forever.** After the first full sweep, all subsequent syncs use `"mode":"incremental"`; they resume from the stored cursor and only pull new pages → minimal re-embedding.
5. **Run extraction** to enrich edges the deterministic ingest can't see:
   ```bash
   curl -sS -X POST http://localhost:8093/v1/extract \
     -H "Authorization: Bearer $AGENT_JWT"
   ```
   Repeat until `ExtractResponse.nodes_seen == 0` (the worklist drains in `limit=50` batches).

**Recovery / partial failure**
- **A sync died mid-sweep.** Just re-issue the same `sync` call. `sync_cursors` resumes per stream; already-landed records are deduped by `content_sha` (`skipped_duplicate` climbs, `docs_ingested` does not); already-embedded docs are deduped by the RAG `Idempotency-Key`. **Re-running is always safe and never double-spends.**
- **One bad record.** `ingest_records` catches per-record exceptions (`stats.errors++`, logs `ingest_record_failed`) so **one poison record never aborts the backfill**. Find it via the structured log's `external_id`, fix the connector/normalizer, re-sync.
- **`errors > 0` but `records_new` advanced.** The good records committed; only the failed ones need a re-run, which the dedup makes cheap.
- **Webhook-landed records show `docs_ingested = 0`.** Expected — the webhook path (`POST /webhooks/{kind}?tenant=<uuid>`) is **graph-only** (no agent JWT to forward to RAG, so `rag=None`). Run an authenticated `sync` (or the worker) to embed the deferred records; their landing already deduped, so only the embed step runs.

**Success criteria:** a second identical `sync` reports `records_new = 0`, `docs_ingested = 0`, `skipped_duplicate = records_seen`, `errors = 0` — proof the backfill is complete and idempotent.

---

### 6.2 Runbook B — Embedding-path (RAG / llms) outage

**Goal:** keep the product serving correct answers while the embedding path (RAG, or the embedding model behind it) is degraded, and recover cleanly without double-spending.

**Symptoms / detection**
- `cypherx_a1_downstream_calls_total{service="rag",result="error"|"rejected"}` climbing.
- `sync` calls return inflated `errors` with logs `rag_ingest_rejected` / `rag_create_kb_failed` (`RagClient._post` raises `SERVICE_UNAVAILABLE` on any `>=400` or transport error).
- Copilot answers still return but with **graph + keyword only** (dense leg empty); a 403 from RAG query is handled as `forbidden=True` (degrade, not fail), other errors raise `SERVICE_UNAVAILABLE` from `RagClient.query`.

**Blast-radius triage — what still works**

| Capability | Status during RAG/embedding outage |
| --- | --- |
| Graph queries (`who_owns`, `what_breaks`, `experts_on`) | **Unaffected** — pure Postgres CTE, no RAG |
| Keyword leg (`find_entities`, `keyword_search` over `fts`) | **Unaffected** — Postgres GIN |
| Copilot dense recall | **Degraded** — RRF runs with the dense leg empty; graph + keyword carry the answer |
| New `sync` / backfill embedding | **Failing** — `ingest_inline` errors; records still **land + normalize** (the graph tx is independent) |
| Extraction (`/v1/extract`) | **Unaffected by RAG**, but fails if **llms-gateway** itself is the outage (it's the chat path) |

**Immediate actions**

1. **Stop the bleed on backfills.** Pause scheduled `sync`/`extract` and the worker so you're not retrying into a dead dependency. Graph-only landing via webhooks can continue (it never touches RAG).
2. **Confirm the seam holds.** Verify copilot still answers from graph + keyword (it should — RAG query degrades, doesn't fail). If copilot is hard-failing, the outage is **llms-gateway** (the copilot's answer call), not RAG — different runbook tier; copilot cannot synthesize without the gateway and will surface `SERVICE_UNAVAILABLE`.
3. **Decouple landing from embedding deliberately.** While RAG is down you can keep ingesting *structure* (graph) with zero embedding cost: drive landing via the webhook path or accept that `sync`'s embed step will fail-per-record while landing succeeds. The graph stays current; embeddings are a **deferred enrichment** you backfill on recovery.

**Recovery**

1. Confirm RAG `/readyz` healthy and the **pinned embedding model is the same** as before (a model change would split the vector space — see §5.3; if it changed, that's a new-KB migration, not a resume).
2. **Re-embed only what's missing.** Re-run `sync` (incremental) for the affected window. `content_sha` + the RAG `Idempotency-Key` guarantee: already-embedded docs are skipped, only the records that failed-to-embed during the outage get embedded now. **No double spend** — the meter only moves for genuinely-new content.
3. For records that landed graph-only during the outage (webhook path), run an authenticated `sync` to embed them; their landing already deduped, so only the embed runs.
4. Re-enable the worker and scheduled syncs.

**Do-not list**
- Do **not** raise `ef_search`/`top_k` past the caps to "compensate" for thin dense results — fix recall with RRF/keyword, not by overrunning RAG.
- Do **not** trigger a full corpus re-embed on recovery — `content_sha` makes that pure waste.
- Do **not** repoint the KB to the `embed` alias to "get embeddings working again" — the pin is load-bearing for a stable vector space.

---

### 6.3 Runbook C — Tenant offboarding / GDPR wipe

**Goal:** completely remove a tenant's data across **all** stores — graph, raw landing, RAG vectors, copilot memory, the cost ledger, and in-flight events — honouring the ownership split (cypherx-a1 owns the graph + landing; SharedCore owns vectors + memory).

**The data map — every place a tenant's bytes live**

| Store | Owner | What's there | How it's removed |
| --- | --- | --- | --- |
| `cypherx_a1.entities`, `edges`, `identities` | app | the knowledge graph (incl. bitemporal history) | `DELETE` under the tenant's RLS context |
| `cypherx_a1.raw_events` | app | immutable source landings (may contain PII) | `DELETE` (the only place the original payload lives inline) |
| `cypherx_a1.connectors`, `connector_secrets`, `sync_cursors` | app | install config + **sealed credentials** + cursors | `DELETE`; rotate/destroy the source token externally |
| `cypherx_a1.extraction_jobs` | app | cost ledger (`llm_call_id`, `cost_usd`) | `DELETE` (after billing has settled — see note) |
| `cypherx_a1.citations`, `rag_kbs`, `resource_acls` | app | provenance links + KB bindings + ACLs | `DELETE` |
| `cypherx_a1.outbox` | app (no RLS) | in-flight events for the tenant | drain or delete by `partition_key = <tenant_id>` |
| **RAG KBs** (`eng-code`, `eng-conversations`, `eng-docs`, `eng-incidents`) | **SharedCore RAG** | the tenant's **vectors + chunk text** | call RAG's tenant/KB deletion in its `/v1` contract |
| **Memory** | **SharedCore Memory** | copilot **episodic** conversational context (`scope=principal_only`) | call Memory's GDPR-wipe in its `/v1` contract |

> The graph is **never** in Memory and **never** in RAG (only opaque text + provenance is in RAG), so there is no cross-store graph leakage to chase. Memory holds only per-principal copilot turns; RAG holds only embedded artifact text. This boundary is what makes the wipe tractable.

**Steps**

1. **Freeze ingestion for the tenant.** Set the connector(s) `status='paused'`, stop scheduled `sync`/`extract`, and stop the worker consuming that tenant's records. No new data must land mid-wipe.
2. **Drain or quarantine the outbox.** Let `OutboxPublisher` finish publishing the tenant's `published_at IS NULL` rows (so downstream billing reconciles), **or** delete them by `partition_key = <tenant_id>` if the wipe must be immediate. Outbox has **no RLS** (cross-tenant queue) so this is a targeted delete by `partition_key`, not an RLS-scoped one.
3. **Wipe SharedCore-owned data first (so nothing re-references it):**
   - **RAG:** for each `rag_kbs` row (`logical_name → kb_id`) of the tenant, invoke RAG's KB/tenant deletion via its `/v1` contract with the tenant's forwarded JWT. This destroys the vectors **and** the chunk text — the only copy of embedded content.
   - **Memory:** invoke Memory's GDPR-wipe for the tenant's principal(s). cypherx-a1 treats memory as best-effort at read/write time, but the **wipe must be confirmed**, not best-effort — verify the delete returns success.
4. **Wipe app-owned data, under the tenant's RLS context.** Run inside the `in_tenant` transaction so RLS scopes every statement to exactly this tenant (FORCE RLS makes cross-tenant deletion architecturally impossible — you cannot fat-finger another tenant):
   ```
   in_tenant(pool, <tenant_id>, fn):
     DELETE FROM cypherx_a1.citations;
     DELETE FROM cypherx_a1.edges;
     DELETE FROM cypherx_a1.entities;          -- after edges (FK-free but order keeps reads sane)
     DELETE FROM cypherx_a1.identities;
     DELETE FROM cypherx_a1.extraction_jobs;    -- see billing note
     DELETE FROM cypherx_a1.sync_cursors;
     DELETE FROM cypherx_a1.connector_secrets;  -- sealed creds gone
     DELETE FROM cypherx_a1.connectors;
     DELETE FROM cypherx_a1.resource_acls;
     DELETE FROM cypherx_a1.rag_kbs;            -- last: it's the index into RAG, needed in step 3
     DELETE FROM cypherx_a1.raw_events;         -- the inline source payloads (PII)
   ```
   (Order: SharedCore deletes in step 3 must read `rag_kbs` first, so app-side `rag_kbs` is deleted **after** the RAG KBs are gone.)
5. **Rotate the source credential externally.** `connector_secrets` held a sealed token; deleting the row removes our copy, but the GitHub PAT / app install must also be revoked at the source.
6. **Verify.** Re-run any read under the tenant's context and confirm zero rows; confirm RAG/Memory return empty for the tenant; confirm no `outbox` rows with that `partition_key`.

**Notes & guards**
- **Billing settlement vs erasure.** `extraction_jobs.cost_usd` / `llm_call_id` is the Contract-19 cost ledger. Confirm the platform billing roll-up has consumed the tenant's `usage.recorded` events **before** deleting the ledger, or you erase un-reconciled cost. The gateway's own cost record is authoritative and is never rewritten by us; deleting our ledger does not refund — settle first.
- **RLS is the safety rail.** Because every app-side delete runs under `app.tenant_id` with FORCE RLS, the wipe physically cannot touch another tenant's rows even with a buggy query. The outbox delete is the one exception (no RLS) and must be scoped by `partition_key` explicitly.
- **No GDPR-wipe endpoint exists yet.** The MVP exposes no `DELETE /v1/tenants/{id}` route; this runbook is operational (driven via the `in_tenant` helper + direct SharedCore `/v1` calls). A first-class admin endpoint is a documented future task — when built, it must follow exactly this ordering.

---

## 7. Operability quick-reference

### 7.1 Metrics to watch (Prometheus, `/metrics`)

| Metric | Tells you |
| --- | --- |
| `cypherx_a1_ingestion_records_total{source,status}` | ingest throughput; `status="new"` vs deduped |
| `cypherx_a1_graph_edges_upserted_total{rel}` | graph write rate per relation |
| `cypherx_a1_extraction_jobs_total{status}` | extraction `completed` vs `failed` |
| `cypherx_a1_downstream_calls_total{service,result}` | RAG/Memory/llms health: `ok|rejected|forbidden|error` |

### 7.2 The cost-control invariants (never break these)

| Invariant | Enforced by |
| --- | --- |
| Re-ingest never re-spends on unchanged content | `content_sha` dedup at all three gates (§4) |
| RAG never re-embeds the same bytes | `Idempotency-Key = "{tenant}:{content_sha}:{kb}"` |
| Extraction never re-spends a node/version | `extraction_jobs` PK `(tenant, node_id, content_sha, extractor_version)` |
| The gateway's cost is authoritative | record `llm_call_id` + `cost_usd`; **never rewrite** (Contract 19) |
| Embedding space stays stable | KB model pinned + immutable in `rag_kbs` (never the `embed` alias) |
| Traversal cost is bounded | `retrieval_max_hops` cap + per-read `LIMIT` + current-slice partial index |
| Cross-tenant data is unreachable | FORCE RLS on every tenant-scoped table; wipe runs under `in_tenant` |

### 7.3 The scale-out seams (built as seams, not yet live)

| Seam | Where | Replaces the synchronous MVP path |
| --- | --- | --- |
| Kafka ingestion/extraction worker | `worker/runner.py`, group `cypherx-cypherxa1-workers`, `worker_max_attempts=3` + `.dlq` | the synchronous `sync`/`extract` request loop |
| `what_breaks` transitive-closure precompute | derived `impact_closure` table, invalidated off `record.normalized` (§3) | the live recursive `impact_of` CTE on every call |
| RAG KB compaction / orphan reclaim | reconcile `citations`/`vector_ref` vs KB, delete unreferenced docs (§5.3) | tolerating orphaned vectors after edits |
| First-class GDPR-wipe endpoint | a future `DELETE` route driving Runbook C's ordering | the operational wipe in §6.3 |
