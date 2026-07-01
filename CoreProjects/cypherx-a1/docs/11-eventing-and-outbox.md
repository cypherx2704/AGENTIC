# Eventing & outbox

> How cypherx-a1 turns durable state changes into Kafka events: the `cypherx.cypherxa1.*` topic catalog, the Contract-5 envelope, the transactional outbox (`enqueue_event` in the same tenant tx, `OutboxPublisher` background drain), Contract-19 usage metering, and the consumer-group worker topology — written from the code in `src/cypherx_a1/db/outbox.py`, `core/config.py`, `ingestion/pipeline.py`, `extraction/extractor.py`, and `db/migrations/20260614_0001__init.sql`.

---

## 1. Why an outbox at all

cypherx-a1 is a **consuming app** (a peer of `xAgent/ax-1`), not a SharedCore service, but it still emits events so that downstream consumers — the platform billing roll-up, observability, and the app's own scale-out ingestion/extraction worker — can react to what it ingests and what it costs. The hard requirement is that **an emitted event and the database mutation it describes can never diverge**. If we ingested a record into the graph but the "record normalized" event got lost, downstream state drifts; if we published an event for a row that then rolled back, downstream state lies.

The transactional outbox pattern solves this by writing the event row **into the same Postgres transaction as the domain write**. Either both commit or neither does. A separate background task (`OutboxPublisher`) then reads committed-but-unpublished rows and pushes them to Kafka (Redpanda) at-least-once, marking each row published on success. Kafka being down never fails a request — the event simply stays durable in `cypherx_a1.outbox` until the next drain tick.

This mirrors the xAgent ax-1 outbox shape deliberately, so the platform has one consistent eventing idiom across consuming apps.

```
┌─────────────────────────── one tenant transaction (in_tenant) ───────────────────────────┐
│  graph_repo.set_vector_ref(...)          ← domain write (entity vector_ref)                │
│  ingest_repo.add_citation(...)           ← domain write (citation)                         │
│  enqueue_event(conn, topic=..., ...)     ← INSERT INTO cypherx_a1.outbox (same conn!)      │
└────────────────────────────────────────── COMMIT ────────────────────────────────────────┘
                                              │
                            (committed, published_at IS NULL)
                                              │
                          OutboxPublisher._drain_once()  every ~2s
                                              │
                                  aiokafka send_and_wait
                                              │
                                ┌─────────────┴─────────────┐
                              success                     failure
                                │                            │
                  UPDATE published_at = NOW()    UPDATE attempts+1, last_error
                                                             │
                                              (attempts ≥ 10) → topic + ".dlq" → published_at = NOW()
```

---

## 2. The Contract-5 envelope

Every event is wrapped in the **Contract-5 envelope** before it lands in the outbox. The envelope is built by `build_envelope(...)` in `src/cypherx_a1/db/outbox.py` and stored as the JSONB `payload` column — fully formed and **ready to publish verbatim** (the publisher does not re-shape it).

```python
# src/cypherx_a1/db/outbox.py
def build_envelope(event_type, tenant_id, trace_id, payload, *, producer_version) -> dict:
    return {
        "event_id":         str(uuid.uuid4()),
        "event_type":       event_type,
        "schema_version":   "1.0.0",
        "produced_at":      _now_iso(),          # RFC3339 millis, "...Z"
        "trace_id":         trace_id,
        "tenant_id":        tenant_id,
        "producer_service": PRODUCER_SERVICE,    # "cypherx-a1"
        "producer_version": producer_version,
        "partition_key":    tenant_id,           # ← Contract 5: partition by tenant
        "payload":          payload,             # event-type-specific body
    }
```

### Envelope fields

| Field | Type | Source / value | Notes |
|-------|------|----------------|-------|
| `event_id` | string (UUID v4) | `uuid.uuid4()` per event | unique per event; consumers dedupe on this |
| `event_type` | string | e.g. `cypherx.cypherxa1.record.normalized` | equals the topic name for produced events |
| `schema_version` | string | const `"1.0.0"` | payload schema version, not the producer version |
| `produced_at` | string (RFC3339) | `_now_iso()` → `2026-06-14T12:34:56.789Z` | UTC, millisecond precision, trailing `Z` |
| `trace_id` | string | `trace.trace_id_var.get()` (Contract 8) | W3C trace id propagated from the originating request |
| `tenant_id` | string (UUID) | the verified JWT tenant — never a request body | identity comes from the token only (Contract 13) |
| `producer_service` | string | const `"cypherx-a1"` (`PRODUCER_SERVICE`) | |
| `producer_version` | string | `settings.service_version` (default `"0.1.0"`) | passed in from the call site |
| `partition_key` | string | **`tenant_id`** | also the Kafka message key — see §5 |
| `payload` | object | event-type-specific (see §4) | additive-only; consumers tolerate unknown fields |

`_now_iso()` is the canonical timestamp formatter:

```python
def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
```

**Forward-compatibility contract.** Per the platform-wide convention, envelope and payload are **additive-only**. Consumers must ignore unknown fields; producers must not remove or repurpose existing ones. A breaking change is a new `schema_version` / new topic, never an in-place edit.

---

## 3. The topic catalog

Topics follow the Contract-5 naming convention `cypherx.<domain>.<entity>.<event-type>`, where the domain segment for this app is **`cypherxa1`** (no separator inside the app name). The prefix is configurable:

```python
# src/cypherx_a1/core/config.py
ingestion_topic_prefix: str = "cypherx.cypherxa1"
usage_topic: str          = "cypherx.cypherxa1.usage.recorded"
```

Every produced topic has a paired **dead-letter topic** formed by appending the literal suffix `.dlq` (`_DLQ_SUFFIX = ".dlq"` in `outbox.py`).

| Topic | Status | Producer / call site | `partition_key` | Purpose |
|-------|--------|----------------------|-----------------|---------|
| `cypherx.cypherxa1.record.normalized` | **emitted (MVP)** | `ingestion/pipeline.py` → `enqueue_event` (same tx as the citation + vector-ref write) | `tenant_id` | one record was normalized into the graph and bound into a RAG KB document |
| `cypherx.cypherxa1.usage.recorded` | **wired, planned** | `usage_topic` config + `record_event(...)` helper (standalone-tx emitter) | `tenant_id` | app-owned usage/cost signal (Contract 19) |
| `cypherx.cypherxa1.raw.landed` | **planned** | worker seam (`raw_events` landing) | `tenant_id` | a raw source artifact landed idempotently, awaiting normalization |
| `cypherx.cypherxa1.extraction.requested` | **planned** | worker seam | `tenant_id` | an extraction job was enqueued for a node |
| `cypherx.cypherxa1.extraction.completed` | **planned** | worker seam | `tenant_id` | LLM knowledge-extraction finished; edges superseded |
| `*.dlq` (one per topic above) | auto | `OutboxPublisher._mark_failure` after 10 attempts | `tenant_id` | poison/undeliverable envelopes |

### What is actually emitted in the MVP

Only **`cypherx.cypherxa1.record.normalized`** is produced in the current runnable slice — it is the single `enqueue_event(...)` call on the synchronous ingestion path. The `usage.recorded` topic is **wired** (the config var and the `record_event` standalone emitter both exist) but is the documented Contract-19 seam, not yet emitted on a hot path. The `raw.landed` / `extraction.*` topics are the **work-topic catalog the scale-out worker will consume and produce** (see §8); the MVP drives ingestion and extraction synchronously through the authenticated API, so those topics are documented intent, not live traffic.

> **Consumed topics:** none in the MVP. cypherx-a1 produces only. The platform guarantee is that this app consumes only `cypherx.tenant.*` events when it later subscribes — it never reaches into another service's topics.

---

## 4. Event payloads

The `payload` object inside the envelope is event-type-specific. Below are the real shapes from the code.

### 4.1 `record.normalized`

Emitted **once per document** as it is bound into a RAG KB, inside the same transaction that sets the entity's `vector_ref` and writes the citation (`ingestion/pipeline.py`):

```python
# src/cypherx_a1/ingestion/pipeline.py  (inside the per-doc _link tx)
await graph_repo.set_vector_ref(conn, entity_id=_eid, vector_ref={"kb_id": _kb_id, "doc_id": _doc_id})
await ingest_repo.add_citation(conn, kb_id=_kb_id, doc_id=_doc_id, chunk_id=None, entity_id=_eid)
await enqueue_event(
    conn,
    topic="cypherx.cypherxa1.record.normalized",   # TOPIC_RECORD_NORMALIZED
    tenant_id=tenant_id,
    trace_id=trace_id,
    event_type="cypherx.cypherxa1.record.normalized",
    payload={
        "source":      record.source,       # e.g. "github"
        "external_id": record.external_id,   # source-native id of the artifact
        "kb_id":       _kb_id,               # RAG knowledge-base id it was ingested into
        "doc_id":      _doc_id,              # RAG document id
        "entity_id":   _eid,                 # graph entity bound to the doc
    },
    producer_version=producer_version,       # settings.service_version
)
```

| Payload field | Meaning |
|---------------|---------|
| `source` | connector/source family (`github` in the MVP) |
| `external_id` | the source-native artifact id |
| `kb_id` | RAG KB the document was ingested into (one of `eng-code` / `eng-conversations` / `eng-docs` / `eng-incidents`) |
| `doc_id` | RAG document id returned by inline ingest |
| `entity_id` | the `cypherx_a1.entities` row the doc is bound to (the graph node) |

Note that the **graph itself never enters the event** — only references (`entity_id`, `doc_id`, `kb_id`). The graph is app-owned and never leaves cypherx-a1's Postgres.

### 4.2 `usage.recorded` (Contract 19 — planned/wired)

The intended payload carries **app-owned usage units keyed to the request**, never the gateway's cost numbers re-stated as authoritative. See §6 for the full Contract-19 discipline. The `record_event(...)` helper exists precisely so a usage signal can be emitted in its **own** tenant transaction (it is an additive side-signal, not part of any domain write):

```python
# src/cypherx_a1/db/outbox.py
async def record_event(pool, *, topic, tenant_id, trace_id, event_type, payload, producer_version):
    """Standalone: insert ONE outbox event in its own tenant tx (additive usage signals)."""
    async def _txn(conn): await enqueue_event(conn, topic=topic, ...)
    await in_tenant(pool, tenant_id, _txn)
```

---

## 5. Partitioning & ordering: `partition_key = tenant_id`

The envelope's `partition_key` is the **`tenant_id`**, and the publisher passes it as the **Kafka message key**:

```python
# src/cypherx_a1/db/outbox.py  (OutboxPublisher._drain_once)
await producer.send_and_wait(topic, value=payload, key=partition_key)
```

with the key serializer:

```python
key_serializer=lambda k: k.encode("utf-8") if k else None,
value_serializer=lambda v: json.dumps(v).encode("utf-8"),
```

Consequences:

- **Per-tenant ordering.** All events for a tenant hash to the same partition, so a consumer sees that tenant's events in produce order. (Across tenants there is no global order — none is needed.)
- **Tenant-balanced load.** Partitions spread tenants across consumers in a group.
- **No PII in the key.** The key is an opaque tenant UUID.

The outbox stores `partition_key` as its own column (`VARCHAR(64)`) in addition to being inside the envelope, so the publisher never has to crack open the JSONB to find the key.

---

## 6. Usage metering — Contract 19

Contract 19 governs how the platform meters usage. The rule cypherx-a1 follows is precise and load-bearing:

> **The app emits its OWN usage, on its OWN topic, in units and request_id — and NEVER rewrites the gateway's cost.**

### The billing key belongs to the gateway

Every LLM call goes through **llms-gateway** (the only path to a provider). The gateway returns an `llm_call_id` and a `cost_usd`; **`llm_call_id` is the billing key**. cypherx-a1 records those numbers verbatim and never recomputes, adjusts, or overrides them:

```python
# src/cypherx_a1/extraction/extractor.py  (header comment)
#  * the gateway's ``llm_call_id`` + ``cost_usd`` are recorded; cypherx-a1 never rewrites
#    the gateway's cost numbers (Contract 19).
```

On the extraction path the gateway's identifiers are persisted into the **cost ledger** (`cypherx_a1.extraction_jobs`) inside the extraction write transaction:

```python
# src/cypherx_a1/extraction/extractor.py
await ingest_repo.record_extraction_job(
    conn, node_id=node_id, content_sha=content_sha, extractor_version=ev,
    edges_extracted=added,
    llm_call_id=completion.llm_call_id,      # gateway's billing key — recorded, never rewritten
    cost_usd=completion.usage.cost_usd,      # gateway's cost — recorded, never rewritten
)
```

### What the app's own usage event carries

The app-owned `cypherx.cypherxa1.usage.recorded` event is for **app-level units the gateway cannot know** (records normalized, nodes/edges upserted, docs ingested, extractions performed). It references the gateway's billing key by **`request_id` / `llm_call_id`** so platform billing can correlate, but it expresses the app's own units. It must **not** re-state `cost_usd` as if cypherx-a1 were the source of truth for cost — that authority lives with the gateway.

| Concern | Owner | Where it lives |
|---------|-------|----------------|
| Per-LLM-call cost (`cost_usd`, `llm_call_id`) | **llms-gateway** | recorded read-only in `extraction_jobs`; correlated by `request_id` in the usage event |
| App units (records/nodes/edges/docs/extractions) | **cypherx-a1** | `payload` of `cypherx.cypherxa1.usage.recorded` |
| Per-MCP-invocation metering of `mcp-eng-memory` | **the calling xAgent's outbox** | NOT this app — the MCP facade is stateless and meters nothing |

This last row matters: `mcp-eng-memory` is a stateless facade with no DB, no Kafka, and no outbox. Tool-invocation metering is the **caller's** (xAgent's) responsibility. cypherx-a1 meters only its own product usage on its own topic.

---

## 7. The transactional outbox table & enqueue path

### 7.1 Table DDL

From `db/migrations/20260614_0001__init.sql`:

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,   -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,   -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cxa1_outbox_unpublished
  ON cypherx_a1.outbox (created_at) WHERE published_at IS NULL;
```

| Column | Type | Role |
|--------|------|------|
| `id` | UUID PK | row identity; the drain loop updates/DLQs by this |
| `topic` | VARCHAR(200) | destination Kafka topic |
| `partition_key` | VARCHAR(64) | `tenant_id`; used as the Kafka message key |
| `payload` | JSONB | the complete Contract-5 envelope, published verbatim |
| `created_at` | TIMESTAMPTZ | enqueue time; drain orders by this (FIFO) |
| `published_at` | TIMESTAMPTZ | NULL until delivered (or DLQ'd); the "done" flag |
| `attempts` | INTEGER | delivery attempts so far; DLQ at ≥ 10 |
| `last_error` | TEXT | truncated last delivery error (`error[:2000]`) |

The **partial index** `idx_cxa1_outbox_unpublished` is the hot-path index: the publisher only ever scans `WHERE published_at IS NULL ORDER BY created_at`, and the index covers exactly that predicate, so the unpublished backlog is cheap to find regardless of how large the published history grows.

### 7.2 The outbox has NO RLS — by design

Every other table in the `cypherx_a1` schema is tenant-scoped with `FORCE ROW LEVEL SECURITY` keyed on `app.tenant_id`. The outbox is the deliberate exception:

```sql
-- outbox is an INTERNAL publish queue drained by a background task across ALL tenants;
-- tenant-RLS would block the drain (the publisher sets no app.tenant_id). Isolation is in
-- the payload, not the row. RLS intentionally NOT enabled on outbox.
ALTER TABLE cypherx_a1.outbox DISABLE ROW LEVEL SECURITY;
```

The reasoning: the `OutboxPublisher` runs as a background task with **no `app.tenant_id` set** — it must drain rows for *all* tenants in one pass. If the outbox had RLS, the publisher would see zero rows. Isolation is preserved because every row's `tenant_id` lives inside the envelope `payload` and is set as the Kafka partition key; cross-tenant leakage is impossible at the consumer because each event self-identifies its tenant. This is the documented "outbox NO RLS" guard — do not enable RLS on this table.

The runtime role grant reflects the publisher's needs (insert to enqueue, select + update to drain/mark) — no delete:

```sql
GRANT SELECT, INSERT, UPDATE ON cypherx_a1.outbox TO cxa1_user;
```

### 7.3 `enqueue_event` — same-transaction insert

`enqueue_event` takes the **caller's** connection and inserts on it, so the row commits or rolls back exactly with the surrounding domain write:

```python
# src/cypherx_a1/db/outbox.py
async def enqueue_event(conn, *, topic, tenant_id, trace_id, event_type, payload, producer_version) -> None:
    """Insert one outbox row on the CALLER's connection (same tx as the domain write)."""
    envelope = build_envelope(event_type, tenant_id, trace_id, payload, producer_version=producer_version)
    await conn.execute(
        "INSERT INTO cypherx_a1.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
        (topic, tenant_id, Jsonb(envelope)),
    )
```

The call site in `ingestion/pipeline.py` passes the *same* `conn` it just used for `set_vector_ref` and `add_citation` — that is the entire correctness argument. The transaction is opened by the RLS helper `in_tenant(pool, tenant_id, fn)` in `db/pool.py`:

```python
# src/cypherx_a1/db/pool.py
async def in_tenant[T](pool, tenant_id, fn) -> T:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
        return await fn(conn)
```

So the domain writes execute under RLS (`app.tenant_id` set) and the outbox insert rides the same transaction — even though the outbox table itself ignores RLS.

### 7.4 `record_event` — standalone-tx emitter

For an **additive side-signal** that is not part of any domain write (the usage event is the canonical case), `record_event` opens its own tenant transaction via `in_tenant` and enqueues a single row. This keeps usage signalling decoupled from the operation it describes while still going through the durable outbox.

---

## 8. `OutboxPublisher` — the background drain

`OutboxPublisher` is started in the app lifespan (`main.py`) when `settings.outbox_publisher_enabled` is true (default), as an asyncio task named `cypherxa1-outbox-publisher`. It owns one long-lived aiokafka producer and loops forever.

### 8.1 Lifecycle

| Method | Behaviour |
|--------|-----------|
| `start()` | `asyncio.create_task(self._run(), name="cypherxa1-outbox-publisher")` |
| `_run()` | loop: `_drain_once()`, then wait up to `poll_interval` (default **2.0s**) or until stopping; **swallows every exception** so the publisher never dies on a transient error |
| `_ensure_producer()` | lazily constructs + starts the `AIOKafkaProducer`; on failure logs `kafka_producer_unavailable` and returns `None` (never raises) |
| `stop()` | sets the stop event, cancels the task, stops the producer best-effort |

The producer is created lazily and its failure is **non-fatal**: if Kafka is unreachable, `_ensure_producer()` returns `None`, `_drain_once()` returns immediately, and the events simply remain durable in the outbox for the next tick. The request path is never coupled to Kafka health.

### 8.2 The drain

```python
# src/cypherx_a1/db/outbox.py  (OutboxPublisher._drain_once)
SELECT id, topic, partition_key, payload, attempts
  FROM cypherx_a1.outbox
 WHERE published_at IS NULL
 ORDER BY created_at
 LIMIT 100
```

For each fetched row:

1. `producer.send_and_wait(topic, value=payload, key=partition_key)`.
2. **On success:** `UPDATE cypherx_a1.outbox SET published_at = NOW() WHERE id = %s`.
3. **On failure:** `_mark_failure(...)` — bump `attempts`, store the truncated error in `last_error`, and continue to the next row (one poison row never blocks the batch).

Up to **100 rows per tick**, ordered by `created_at` (FIFO within a tenant's partition). This is **at-least-once** delivery: a crash after the Kafka send but before the `published_at` update will re-send on the next drain, which is why consumers must dedupe on `event_id`.

### 8.3 DLQ — the `.dlq` suffix

After **10 attempts** (`_MAX_ATTEMPTS = 10`) `_mark_failure` routes the envelope to the dead-letter topic by appending `_DLQ_SUFFIX = ".dlq"`, then marks the row published so the main loop stops retrying it:

```python
# src/cypherx_a1/db/outbox.py  (_mark_failure tail)
if new_attempts >= _MAX_ATTEMPTS and self._producer is not None:
    await self._producer.send_and_wait(topic + _DLQ_SUFFIX, value=payload, key=partition_key)
    # UPDATE ... SET published_at = NOW() WHERE id = ...
    logger.warning("outbox_row_dlq", row_id=str(row_id), topic=topic)
```

So `cypherx.cypherxa1.record.normalized` poison rows land on `cypherx.cypherxa1.record.normalized.dlq`, and likewise for every other topic. The DLQ send preserves the original `partition_key`, so a tenant's failures stay co-located on the DLQ as well. If even the DLQ send fails, the row keeps `published_at IS NULL` and `last_error` set, and it is logged `outbox_dlq_failed` for operator follow-up.

### 8.4 Constants summary

| Constant (`db/outbox.py`) | Value | Meaning |
|---------------------------|-------|---------|
| `PRODUCER_SERVICE` | `"cypherx-a1"` | envelope `producer_service` |
| `_DLQ_SUFFIX` | `".dlq"` | appended to the topic for dead-lettered rows |
| `_MAX_ATTEMPTS` | `10` | attempts before DLQ |
| `poll_interval` (ctor default) | `2.0` (seconds) | drain cadence |
| `LIMIT` per drain | `100` | rows per tick |
| `last_error` truncation | `error[:2000]` | stored error length cap |

### 8.5 Observability of the drain

The publisher emits structured (Contract-6 JSON) log events that operators can alert on:

| Log event | When |
|-----------|------|
| `kafka_producer_started` | producer connected |
| `kafka_producer_unavailable` | producer could not start (Kafka down) — drain skipped this tick |
| `outbox_drain_error` | unexpected error in a drain pass (loop continues) |
| `outbox_row_dlq` | a row exceeded 10 attempts and was dead-lettered |
| `outbox_dlq_failed` | even the DLQ send failed |
| `kafka_producer_stop_failed` | producer failed to stop cleanly on shutdown |

A growing count of rows with `published_at IS NULL AND attempts > 0`, or any traffic on a `*.dlq` topic, is the signal that delivery is degraded.

---

## 9. Consumer-group worker topology

The **scale-out async path** is the ingestion/extraction worker (`src/cypherx_a1/worker/runner.py`), selected by `CYPHERXA1_RUN_WORKER=1` and configured by:

```python
# src/cypherx_a1/core/config.py
worker_enabled: bool          = True
ingestion_topic_prefix: str   = "cypherx.cypherxa1"
ingestion_consumer_group: str = "cypherx-cypherxa1-workers"
worker_max_attempts: int      = 3
```

The intended topology is a Redpanda **consumer group** (`cypherx-cypherxa1-workers`) over the work topics — the pipeline a source artifact flows through:

```
raw.landed ──► record.normalized ──► extraction.requested ──► extraction.completed
```

Each worker in the group:

- joins the consumer group, so partitions (and therefore **tenants**, since `partition_key = tenant_id`) are balanced across workers and a tenant's events stay ordered on one consumer;
- re-uses the **same** `ingestion.pipeline` and `extraction.extractor` functions the synchronous API path uses, under a **service-minted principal** (no inbound agent JWT), so there is exactly one code path for both modes;
- retries up to `worker_max_attempts` (3) before dead-lettering — mirroring the rag-service ingestion-worker split.

### Current status (MVP)

The worker is a **documented seam, not a live consumer**. The runnable MVP drives ingestion and extraction **synchronously** through the authenticated API:

- `POST /v1/connectors/{kind}/sync` — runs the connector + `ingest_records(...)` pipeline (emits `record.normalized` via the outbox).
- `POST /v1/extract` — runs the LLM extraction pass (writes the `extraction_jobs` cost ledger).

`worker/runner.py` currently logs `worker_started` and idles on a 30-second heartbeat (`worker_heartbeat`) so the worker process is a no-op rather than a crash when selected. The Kafka consumer loop is wired in Phase 1.5. Until then, the `raw.landed` / `extraction.requested` / `extraction.completed` topics are the documented work-topic catalog the worker will consume and produce; only `record.normalized` (and the planned `usage.recorded`) are produced today.

> **Webhook caveat.** The webhook path (`POST /webhooks/{kind}?tenant=<uuid>`, signature-verified) is **graph-only**: there is no inbound agent JWT to forward to RAG, so document embedding is deferred to an authenticated sync or the worker. A webhook therefore normalizes into the graph (and can emit `record.normalized`-style graph signals via the outbox) but does not itself perform RAG ingestion.

---

## 10. End-to-end: one ingested document

Putting it together, here is the full life of an event for a single GitHub artifact ingested via `POST /v1/connectors/github/sync`:

1. The connector yields canonical records; `ingest_records(...)` normalizes each into graph entities/edges and resolves its RAG KB.
2. For each document, RAG inline-ingest returns a `doc_id` (idempotency key `{tenant}:{content_sha}:{kb}`).
3. Inside **one `in_tenant` transaction** (`app.tenant_id` set for RLS): `set_vector_ref` on the entity, `add_citation`, and `enqueue_event(... record.normalized ...)` — all on the same `conn`. **Commit.** The graph write and the outbox row are now atomic.
4. `OutboxPublisher` drains the row within ~2s, publishes the Contract-5 envelope to `cypherx.cypherxa1.record.normalized` keyed by `tenant_id`, and stamps `published_at`.
5. If Kafka is down or the send fails 10 times, the envelope goes to `cypherx.cypherxa1.record.normalized.dlq`; otherwise downstream consumers (and, in Phase 1.5, the worker group) react in per-tenant order, deduping on `event_id`.
6. Separately, LLM extraction records the gateway's `llm_call_id` + `cost_usd` read-only into `extraction_jobs`; the app's own `usage.recorded` signal (Contract 19, planned) references the gateway's billing key but carries the app's own units — never the gateway's cost rewritten.

---

## 11. Invariants (do NOT break)

- **Same-transaction enqueue.** `enqueue_event(conn, ...)` MUST be called on the caller's connection, in the same transaction as the domain write. Never enqueue from a fresh connection alongside an unrelated domain commit — that reintroduces divergence.
- **Outbox has NO RLS.** Do not enable `ROW LEVEL SECURITY` on `cypherx_a1.outbox`; the publisher drains across all tenants with no `app.tenant_id`. Isolation lives in the payload + partition key.
- **`partition_key = tenant_id`.** The envelope partition key and the Kafka message key are both the tenant id. Do not partition on anything else (it would break per-tenant ordering and tenant-balanced consumption).
- **Never rewrite the gateway's cost (Contract 19).** Record `llm_call_id` + `cost_usd` verbatim; the app emits its OWN units on its OWN `usage.recorded` topic. MCP-invocation metering belongs to the calling xAgent, not to `mcp-eng-memory`.
- **Additive-only envelopes/payloads.** Add fields, never remove or repurpose; a breaking change is a new `schema_version` / new topic.
- **Identity from the JWT only.** `tenant_id` / `trace_id` in the envelope come from the verified token and the active trace context — never from a request body.
- **Consume only `cypherx.tenant.*`.** When cypherx-a1 later subscribes to platform events, it consumes only the tenant-domain topics; it never reads another service's private topics.
