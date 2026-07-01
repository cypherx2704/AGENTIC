# Phase 5 — SharedCore / RAG
> **Status:** ⏳ Pending | **Depends On:** Phase 0, 1, 2, 3 **+ LLMs pre-work package WP06** (declared dependency — see Amendment Log) | **Blocks:** Phase 8, 9 (enhanced)
> **First Cycle:** 📋 Not required for first cycle — xAgent can work without RAG initially. Required before Skills (Phase 8).

## Amendment Log (2026-06 — pre-build reconciliation)

- **LLMs pre-work package (WP06) declared as an explicit dependency.** Phase 5 hard-depends on LLMs surfaces that are deferred in the Phase 3 first-cycle build: `POST /v1/embeddings` (256-item / 25 MiB caps, mock provider, usage + outbox), `GET /v1/models` with `embedding_dim`, the `embed` alias + embedding pricing seeds, and Valkey-backed Idempotency-Key replay. The RAG build may NOT start ahead of WP06.
- **Component 10 bootstrap fixed:** the platform-skills KB bootstrap now inserts the default `(tenant,'*')` `rag.kb_acls` row in the SAME transaction (the old direct-SQL bootstrap bypassed Component 5c's API-path default — the KB would have been readable by NO ONE once ACL enforcement shipped); and the bootstrap is **lazy-with-retry** with an env-pinned `EMBEDDING_MODEL_RESOLVED`/`EMBEDDING_DIM` fallback — `/readyz` requires only that the bootstrap loop is RUNNING, not a live LLMs call (the old spec created a circular cold start against LLMs' soft-dep classification).
- **Bootstrap-tenant consumer deleted** (checklist item had no design behind it): the Component 5e adapter resolver treats a missing `rag.tenant_backends` row as `backend_type='pgvector'` and write-throughs the row on first touch.
- **`rag.documents` bucket-prefix CHECK constraint DROPPED.** The `source_uri LIKE 's3://cypherx-rag-%/...'` CHECK hardcoded a bucket name into the schema layer (nothing-hardcoded violation; unbuildable against MinIO). Bucket/prefix validation is now app-layer config: the service validates `source_uri` at write time against the env-driven `S3_BUCKET`/`S3_ENDPOINT` and the JWT-derived tenant prefix.
- **Compose-parity runtime subsection added** (first-cycle runtime = docker compose + Neon + Valkey + Redpanda + MinIO): `S3_BUCKET`/`S3_ENDPOINT`/`S3_SSE_MODE ∈ {none,kms}` fully env-driven; an idempotent `topics-init` compose job (`rpk topic create`) stands in for Terraform-provisioned topics; Kong-fronted external auth and K8s/ArgoCD deploy are the **cloud (deploy-target) form** — first cycle verifies external agent JWTs directly against Auth JWKS and runs as a compose service with healthchecks. The affected ⚡ checklist items are restated accordingly.
- **Component 5d metering de-coupled from LLMs pricing (Contract-14 single-owner rule):** the live cross-schema join against LLMs `provider_pricing` is DELETED. Usage events carry **units + `request_id` ONLY**; RAG unit costs live in `rag.pricing` (admin-managed); embedding provider cost is metered by the LLMs gateway under the same `request_id` and de-duplicated downstream at billing rollup.
- **Quota enforcement single-owner dedupe:** quota ENFORCEMENT is ⚡ first-cycle in THIS phase (storage bytes, KB/document caps, op-rate windows — see the ⚡ checklist). The duplicate 📋 "Per-tenant storage quotas" item is DELETED (tombstoned); Phase 13 Domain 3 only TUNES limit values and owns nothing canonical.

---

## Phase Overview

SharedCore/RAG is the **universal retrieval-augmented generation service**. Any agent can ingest documents and retrieve relevant context chunks before calling an LLM. The platform uses RAG internally to power the skill retrieval system (Phase 8).

**Deliverable:** A running RAG service capable of ingesting PDF and Markdown documents, chunking and embedding them, and returning relevant chunks via semantic search. The Skills system is a direct consumer of this service.

> **Declared dependency — LLMs pre-work package (WP06).** This phase consumes LLMs surfaces that are NOT part of the Phase 3 first-cycle build until WP06 lands: `POST /v1/embeddings` (256-item / 25 MiB caps, mock provider, usage + outbox), `GET /v1/models` with `embedding_dim`, the `embed` alias + embedding pricing seeds, and Valkey-backed Idempotency-Key replay. **The RAG build may not start ahead of WP06.**

> 🏗️ **Service Architecture Note:** The internal architecture of the RAG service (ingestion pipeline design, async job runner, chunking strategy implementation, vector store client abstraction) must be planned separately before implementation begins.

---

## High Level Design

### System Context

```
                        ┌──────────────────────────────────────────┐
                        │              RAG SERVICE                  │
                        │                                           │
  xAgent ──────────────►│  POST /v1/knowledge-bases/{id}/ingest    │
  Skills System ────────│  POST /v1/knowledge-bases/{id}/query     │
  Platform Mgmt ────────│  GET  /v1/knowledge-bases                │
  External Devs ────────│  GET  /v1/knowledge-bases/{id}/status    │
                        └──────────────┬────────────────────────────┘
                                       │
               ┌───────────────────────┼──────────────────────┐
               ▼                       ▼                       ▼
        Ingestion Queue          LLMs Gateway              pgvector
        (Kafka + workers)        (embeddings)          (vector + metadata
                                                         storage)
```

### Document Ingestion Pipeline

```
Document uploaded (POST /ingest)
  │
  ▼
Job queued (Kafka: internal ingestion topic or in-memory queue)
  │
  ▼
Ingestion Worker:
  ├── 1. Parse document (extract raw text from PDF/Markdown/HTML/etc.)
  ├── 2. Clean text (normalise whitespace, remove artifacts)
  ├── 3. Chunk text (strategy selected per knowledge-base config)
  ├── 4. Embed chunks in batches (call LLMs Gateway /v1/embeddings — see batch policy below)
  └── 5. Store: vector + metadata in pgvector, document record in PostgreSQL
  │
  ▼
Status: completed / failed (queryable via /status endpoint)
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first to unblock Phase 8 (Skills). 📋 items implement after first cycle.

---

### Component 1 — Knowledge Base Management ⚡

**PostgreSQL (`rag.knowledge_bases`):**
```sql
CREATE TABLE rag.knowledge_bases (
  kb_id                     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                 UUID         NOT NULL,
  name                      VARCHAR(255) NOT NULL,
  description               TEXT,
  chunking_strategy         VARCHAR(50)  NOT NULL DEFAULT 'sentence',
                            -- fixed | sentence | semantic | recursive
  chunk_size                INTEGER      NOT NULL DEFAULT 512,
  chunk_overlap             INTEGER      NOT NULL DEFAULT 50,
  embedding_model_alias     VARCHAR(100) NOT NULL DEFAULT 'embed',  -- requested alias
  embedding_model_resolved  VARCHAR(100) NOT NULL,                  -- literal model id, resolved at creation
  embedding_dim             INTEGER      NOT NULL,                  -- e.g., 1536 (resolved at creation)
  status                    VARCHAR(20)  NOT NULL DEFAULT 'active',
  created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  UNIQUE (tenant_id, name)
);

-- Tenant isolation (Contract 13):
ALTER TABLE rag.knowledge_bases ENABLE ROW LEVEL SECURITY;
CREATE POLICY kb_tenant_isolation ON rag.knowledge_bases FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> **Embedding alias is resolved at KB creation and never re-resolved.**
> At `POST /v1/knowledge-bases`, the service calls LLMs gateway `GET /v1/models`
> to resolve `embedding_model_alias` to a literal model ID and persist both that
> ID and its `embedding_dim`. If the alias is later repointed in LLMs gateway,
> existing KBs continue using the originally-resolved model. The resolved fields
> are immutable post-creation — there is no UPDATE path that touches them.

> **Counter columns removed.** Previous draft kept `document_count` and
> `chunk_count` as denormalised columns. Parallel ingestion workers UPDATEing
> the same row create lost-update races. Counts are now computed on demand:
> the status endpoint runs `SELECT COUNT(*)` against `documents` / `chunks`.
> At expected first-cycle scale (≤ 1M chunks per KB), this is a sub-100ms query.

**API:**
```
POST   /v1/knowledge-bases                     Create KB       ⚡
GET    /v1/knowledge-bases                     List KBs        ⚡
GET    /v1/knowledge-bases/{kb_id}             Get KB details  ⚡
DELETE /v1/knowledge-bases/{kb_id}             Delete KB       📋
```

---

### Component 2 — Document Ingestion ⚡

**PostgreSQL (`rag.documents`):**
```sql
CREATE TABLE rag.documents (
  doc_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  kb_id        UUID         NOT NULL REFERENCES rag.knowledge_bases(kb_id),
  tenant_id    UUID         NOT NULL,
  name         VARCHAR(500) NOT NULL,
  source_type  VARCHAR(50)  NOT NULL,   -- pdf | markdown | html | text | url
  source_uri   TEXT,                    -- s3://<S3_BUCKET env>/<tenant_id>/<doc_id>/<filename>
  status       VARCHAR(20)  NOT NULL DEFAULT 'pending',
                            -- pending | processing | completed | failed
  attempts     INTEGER      NOT NULL DEFAULT 0,
  error_msg    TEXT,
  metadata     JSONB        NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ

  -- (bucket-prefix CHECK constraint on source_uri DROPPED 2026-06 — see Amendment Log.
  --  Bucket/prefix validation is APP-LAYER CONFIG: the service validates source_uri at
  --  write time against the env-driven S3_BUCKET/S3_ENDPOINT and the JWT-derived tenant
  --  prefix. Bucket names are never schema literals.)
);

CREATE INDEX idx_documents_kb_id     ON rag.documents(kb_id);
CREATE INDEX idx_documents_status    ON rag.documents(status);

ALTER TABLE rag.documents ENABLE ROW LEVEL SECURITY;
CREATE POLICY documents_tenant_isolation ON rag.documents FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

**S3 bucket layout (single bucket per env, prefix-isolated):**

```
Bucket: cypherx-rag-<env>             (per-env: dev, staging, prod)
Layout: s3://cypherx-rag-<env>/<tenant_id>/<doc_id>/<filename>

Bucket policy: deny all public access; SSE-KMS required (cypherx-rag-<env> CMK alias).
Lifecycle:
  - Multipart-upload aborts cleaned after 7 days.
  - Documents with no associated rag.documents row (>7 days old) cleaned (orphan sweep).
  - Documents older than 90 days → S3 Standard-IA storage class.

rag-service IRSA role:
  s3:PutObject, s3:GetObject, s3:DeleteObject on cypherx-rag-<env>/* only.
  kms:GenerateDataKey, kms:Decrypt on the rag CMK only.
  No s3:ListBucket on the root (prevents tenant enumeration).
```

> **Compose parity (first cycle — see Amendment Log and the Compose-Parity Runtime subsection):** the block above is the **cloud (deploy-target) form**. First cycle the bucket is a **MinIO** bucket created by an idempotent init job; `S3_BUCKET`, `S3_ENDPOINT`, and `S3_SSE_MODE ∈ {none,kms}` are fully env-driven (`none` against MinIO; `kms` activates the SSE-KMS condition in the cloud form); IRSA scoping is replaced by MinIO static credentials from env; lifecycle/orphan sweeps run as a scheduled script. The prefix layout `<tenant_id>/<doc_id>/<filename>` is identical in both forms.

**Ingestion API surface (⚡ — pre-signed URL pattern, NOT multipart-to-service):**

```
1. POST /v1/knowledge-bases/{kb_id}/ingest/upload-url
   Body: { "filename": "manual.pdf", "size_bytes": 4521234, "content_type": "application/pdf" }

   Server-side validation BEFORE generating a URL:
     - size_bytes ≤ 100 MiB (first-cycle cap; rejection: VALIDATION_ERROR)
     - content_type ∈ allowlist {application/pdf, text/markdown, text/plain}
     - tenant_id is derived from X-Forwarded-Agent-JWT — NEVER from request body
       (Contract 13 anti-pattern guard)

   Pre-signed URL is generated with HARD policy conditions (S3 enforces, not client):
     - key                              = exact <tenant_id>/<doc_id>/<sanitised_filename>
     - Content-Length-Range             = [size_bytes, size_bytes]   (exact size)
     - Content-Type                     = exact match
     - x-amz-server-side-encryption     = aws:kms
     - x-amz-server-side-encryption-aws-kms-key-id = <rag CMK alias>
     - Expiry: 900 s

   Response:
   {
     "upload_url": "https://cypherx-rag-<env>.s3.amazonaws.com/...&X-Amz-Signature=...",
     "doc_id":     "<uuid>",
     "expires_in": 900
   }

2. Client PUTs the file directly to upload_url. S3 rejects on any condition mismatch.

3. POST /v1/knowledge-bases/{kb_id}/ingest/finalize
   Header (RECOMMENDED): Idempotency-Key: <client-generated-uuid>
   Body: { "doc_id": "<uuid>" }
   → Server HeadObject's the expected key; rejects if absent, wrong size, or wrong tenant prefix.
   → Enqueues ingestion via the outbox (see below); response:
       { "doc_id": "...", "status": "pending" }
```

> **Idempotency (Contract 9 — MANDATORY for `/ingest/finalize`, RECOMMENDED for `/ingest/upload-url`):**
> Finalize triggers embedding charges via LLMs Gateway. A client network blip + retry on
> `/finalize` causes duplicate chunks in pgvector and double LLM cost. Same pattern as Phase 3:
>
> ```
> Key:   rag-idemp:{tenant_id}:{kb_id}:finalize:{idempotency_key}
> TTL:   24h (Valkey, SET NX EX 86400)
> Value: { "status": "in_flight" | "completed",
>          "doc_id": "<uuid>", "http_status": 200,
>          "body_compressed": "<gzip+base64 of response>" }
> ```
> - Hit (`completed`) → return cached body with `Idempotent-Replay: true`. NO new outbox row,
>   NO duplicate enqueue.
> - Hit (`in_flight`) → return 409 `IDEMPOTENT_REQUEST_IN_FLIGHT` with `Retry-After: 2`.
> - Miss → SET NX, proceed, write `completed` after the outbox transaction commits.
> - Valkey outage → FAIL OPEN with telemetry (`rag_idempotency_skipped_total`), log WARN.
>   Acceptable because the worker-side dedup (next bullet) is the secondary defence.
>
> **Worker-side dedup (defence in depth):** the worker uses `(doc_id, content_sha256)` as a
> natural key when writing chunks. A re-enqueued ingestion that races past Valkey sees
> existing chunks for the same content hash and skips re-embedding. This is cheap
> insurance for the Valkey-outage window.

Why pre-signed URL with strict conditions: pushing 50 MB PDFs through Kong + a service pod wastes CPU, memory, and the gateway's request buffer. But an unconditioned pre-signed URL is a capability the client can abuse — *all* of the conditions above are required to keep it safe (wrong tenant prefix, oversize upload, plaintext storage are all blocked by S3 itself, not by hopeful app code).

**For small inline payloads** (markdown, plain text ≤ 100 KiB), keep a convenience endpoint:
```
POST /v1/knowledge-bases/{kb_id}/ingest/inline
Body: { "name": "...", "content": "<text>", "source_type": "markdown" }

Server-side enforced: byte-length of content ≤ 100 KiB; over-limit → VALIDATION_ERROR.
Server writes content to S3 at the standard key path, then enqueues like the upload flow.
```

**Ingestion queue (⚡ — Kafka, not in-memory):**
- Topic: `cypherx.rag.ingestion.requested` (consumer group: `cypherx-rag-ingestion-workers`).
- **Topic provisioning:** owned by Phase 5 via `environments/<env>/rag-topics/terragrunt.hcl`
  using the same `Mongey/kafka` provider as Phase 1 Component 17. Config:
  `partitions: 6, replication: 3, min.insync.replicas: 2, cleanup.policy: delete, retention: 7 days`.
  Paired DLQ `cypherx.rag.ingestion.requested.dlq` (same partitions, 30-day retention).
  Auto-creation is FORBIDDEN — explicit provisioning only.
- Why Kafka not in-memory: worker pod crashes mid-ingestion would otherwise lose the job; with Kafka, the offset is committed only after successful indexing.
- Worker concurrency tuned per worker pod (default: 4 parallel docs per pod); HPA scales pods on consumer-group lag.

**`cypherx.rag.ingestion.requested` payload (matches Contract 5 envelope; payload field below):**
```json
Topic:         cypherx.rag.ingestion.requested
Partition key: tenant_id
Produced by:   rag-service (from /ingest/finalize, via outbox)
Payload (inside Contract 5 envelope's "payload" field):
{
  "doc_id":                    "<uuid>",
  "kb_id":                     "<uuid>",
  "tenant_id":                 "<uuid>",
  "source_uri":                "s3://<S3_BUCKET env>/<tenant_id>/<doc_id>/<filename>",
  "source_type":               "pdf",                    // pdf | markdown | text
  "embedding_model_resolved":  "text-embedding-3-small", // pinned at KB creation
  "embedding_dim":             1536,
  "chunking_strategy":         "sentence",
  "chunk_size":                512,
  "chunk_overlap":             50,
  "request_id":                "<uuid>",                 // = X-Request-ID at /ingest/finalize time
  "trace_id":                  "<uuid>"
}
```

> **Worker contract:** the message is the authoritative work-order. The worker MUST NOT
> re-read embedding/chunking config from `rag.knowledge_bases` — config is captured into
> the payload at finalize time. The only DB read on the hot path is the document row for
> status transitions. This makes mid-flight KB config changes safe (in-flight jobs use
> snapshotted config; new jobs use new config) and removes a per-message DB roundtrip.

**Poison-pill / DLQ handling (REQUIRED — a corrupt PDF must not block the consumer group forever):**
```
On ingestion failure:
  1. Increment rag.documents.attempts; capture error_msg.
  2. If attempts < 3:           → leave offset uncommitted, will be redelivered.
                                  (Exponential backoff: 30s, 120s, 480s using Kafka's
                                   pause-then-resume or a delay queue.)
  3. If attempts == 3:          → publish to cypherx.rag.ingestion.requested.dlq with
                                  {doc_id, kb_id, tenant_id, error_msg, attempts},
                                  set rag.documents.status = 'failed',
                                  emit cypherx.rag.ingestion.failed via outbox,
                                  commit offset, continue.
```

**Embedding batch policy (worker → LLMs Gateway `/v1/embeddings`):**
- Batch chunks into requests of **≤ 128 items AND ≤ 8 MiB serialized body**, whichever
  hits first. These are deliberately well under Phase 3's hard limits (256 items /
  25 MiB) to leave headroom for retries and provider-side caps.
- Batches preserve chunk order (`chunk_index` ascending) so reassembly into
  `rag.chunks` is deterministic and resumable.
- The worker forwards the originating `X-Request-ID` and `traceparent` on every
  embedding call so usage records on the LLMs side join cleanly to the ingestion
  document (Phase 3 usage_records.request_id = this finalize call's request_id).
- Use the `Idempotency-Key` header on each embedding batch — set to a deterministic
  value `embed:{doc_id}:{batch_first_chunk_index}` — so a worker pod crash + restart
  does NOT double-bill the tenant for the same batch. The LLMs gateway returns the
  cached embedding on replay (Phase 3 idempotency block).
- On 429 from LLMs Gateway: exponential-jitter backoff per Contract 9 `Retry-After`
  header. Resume from the failed batch index — do NOT restart the document.
- On 5xx mid-batch: same backoff; after 3 batch retries fail, raise the document
  attempts counter (Component 2 poison-pill flow handles further escalation).

**Supported source types in first cycle:**
- PDF (via Apache Tika or pdfplumber)
- Markdown
- Plain text

**📋 Full enterprise sources:**
- HTML (with scraping)
- JSON / CSV (one row → one chunk variant)
- URL (web scrape via tool-browser)
- S3 object reference (skip upload — already in S3)
- Webhook push (external systems push documents)

---

### Component 3 — Chunking Engine ⚡

**First cycle strategies:**

| Strategy | How it works |
|----------|-------------|
| `fixed` | Split every N characters with M overlap |
| `sentence` | Split on sentence boundaries (spaCy or simple regex) |

**📋 Full enterprise strategies:**

| Strategy | How it works |
|----------|-------------|
| `semantic` | Detect topic shifts using embedding similarity |
| `recursive` | Chapter → Section → Paragraph hierarchy |
| `code` | AST-aware splitting for code files |

**Chunk data model (pgvector — one table per supported embedding dimension):**

Different embedding models produce vectors of different dimensions (OpenAI `text-embedding-3-small` = 1536, `-large` = 3072, Anthropic embeddings (future) = TBD). A single `vector(N)` column cannot hold multiple dimensions, so we shard tables by dimension:

```sql
-- Common metadata table — one row per chunk, dimension-agnostic
CREATE TABLE rag.chunks (
  chunk_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id          UUID         NOT NULL REFERENCES rag.documents(doc_id) ON DELETE CASCADE,
  kb_id           UUID         NOT NULL REFERENCES rag.knowledge_bases(kb_id) ON DELETE CASCADE,
  tenant_id       UUID         NOT NULL,
  content         TEXT         NOT NULL,
  chunk_index     INTEGER      NOT NULL,
  embedding_model VARCHAR(100) NOT NULL,   -- e.g., "text-embedding-3-small"
  embedding_dim   INTEGER      NOT NULL,   -- 1536 | 3072 | ...
  metadata        JSONB        NOT NULL DEFAULT '{}',
                  -- { "page": 3, "section": "Introduction", "source_uri": "..." }
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_chunks_kb_id        ON rag.chunks(kb_id);
CREATE INDEX idx_chunks_doc_id       ON rag.chunks(doc_id);
CREATE INDEX idx_chunks_metadata_gin ON rag.chunks USING gin (metadata jsonb_path_ops);
-- jsonb_path_ops: smaller, faster index for @> queries (the typical filter pattern).

ALTER TABLE rag.chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY chunks_tenant_isolation ON rag.chunks FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Per-dimension vector tables — one row per chunk per dimension supported
CREATE TABLE rag.chunk_vectors_1536 (
  chunk_id  UUID         PRIMARY KEY REFERENCES rag.chunks(chunk_id) ON DELETE CASCADE,
  tenant_id UUID         NOT NULL,
  kb_id     UUID         NOT NULL,
  embedding vector(1536) NOT NULL
);
CREATE INDEX idx_chunk_vectors_1536_hnsw
  ON rag.chunk_vectors_1536 USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

ALTER TABLE rag.chunk_vectors_1536 ENABLE ROW LEVEL SECURITY;
CREATE POLICY chunk_vectors_1536_tenant_isolation ON rag.chunk_vectors_1536 FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Same template for 3072, etc. — added as new models are onboarded.
-- HNSW preferred over IVFFlat for better recall and no training step.
```

> **Storage estimate (operator awareness):**
> A 1536-dim float32 vector is ~6 KiB. With HNSW overhead (~3×) that's ~24 KiB per chunk
> at-rest. 1M chunks per KB ≈ **24 GiB**. Per-tenant storage quotas ARE enforced first
> cycle (⚡ checklist; limits from Auth Contract-19) — this estimate sizes the cluster
> headroom behind those quotas. Operators must still monitor free space and chunk-count
> growth — quota limits should be tuned (Phase 13) before sustained large-corpus ingest.

A KB's `embedding_model` (and therefore `embedding_dim`) is **fixed at creation**. Switching models requires a re-ingestion job; the data model never asks "which vector column to query" — it picks the table by KB's `embedding_dim`.

---

### Component 4 — Retrieval / Query ⚡

**Two call paths (internal vs external — first cycle):**
```
External (developer hitting the service from outside):
  Authorization: Bearer <agent-jwt>           ← verified by the service directly against
                                                Auth JWKS (compose parity — first cycle);
                                                Kong's JWT plugin in front is the cloud form

Internal (xAgent, Skills service inside the cluster):
  Authorization:         Bearer <service-jwt> ← Contract 12 service token
  X-Forwarded-Agent-JWT: <agent-jwt>          ← agent identity preserved across the hop
  traceparent:           00-<trace-id>-...    ← W3C trace context, Contract 8

Identity (tenant_id, agent_id) is derived from the JWT chain — NEVER from the request
body. If the body contains agent_id / tenant_id / trace_id, gateway returns 400
(Contract 13 anti-pattern guard, same as Phases 2/3/4).
```

**Query Request:**
```json
POST /v1/knowledge-bases/{kb_id}/query

{
  "query":       "What is the refund policy for enterprise plans?",
  "top_k":       5,                              -- max 100 (server-enforced)
  "min_score":   0.7,
  "filters":     { "metadata.source_type": "pdf" },
  "search_mode": "dense",                        -- dense | sparse (📋) | hybrid (📋)
  "ef_search":   100                             -- optional: per-query HNSW recall/latency knob
}
```

**Query Response:**
```json
{
  "results": [
    {
      "chunk_id":   "<uuid>",
      "doc_id":     "<uuid>",
      "content":    "Enterprise plan customers may request a full refund within 30 days...",
      "score":      0.89,
      "metadata":   { "page": 12, "section": "Refund Policy" },
      "source":     { "name": "enterprise-tos.pdf", "uri": "s3://..." }
    }
  ],
  "query_id":    "<uuid>",
  "duration_ms": 45
}
```

> `duration_ms` (was `latency_ms`) — matches Contract 5 and the Phase 3 naming we
> standardised on. Cross-service consistency for SDK ergonomics.

**Retrieval implementation (pgvector) — TWO-PASS CTE (HNSW-friendly):**
```sql
-- IMPORTANT: app sets the per-request RLS context first.
-- Inside the request handler's transaction:
BEGIN;
SET LOCAL app.tenant_id = $tenant_id;
SET LOCAL hnsw.ef_search = COALESCE($ef_search, 100);  -- per-query recall knob

WITH candidates AS (
  SELECT c.chunk_id, c.content, c.metadata, c.doc_id,
         cv.embedding <=> $query_embedding AS distance
  FROM rag.chunk_vectors_1536 cv
  JOIN rag.chunks c USING (chunk_id)
  WHERE c.kb_id = $kb_id                       -- RLS gates tenant_id automatically
    AND ($filters IS NULL OR c.metadata @> $filters)
  ORDER BY cv.embedding <=> $query_embedding   -- HNSW uses this; no calc in WHERE
  LIMIT $top_k * 2                             -- buffer for post-filter
)
SELECT chunk_id, content, metadata, doc_id, 1 - distance AS score
FROM candidates
WHERE 1 - distance >= $min_score
ORDER BY distance
LIMIT $top_k;
COMMIT;
```

> **Why the CTE rewrite:** The previous draft had `WHERE 1 - (embedding <=> $vec) >= $min_score`
> in the same clause as `ORDER BY embedding <=> $vec LIMIT $top_k`. The expression-on-indexed-
> column WHERE clause stops pgvector's HNSW index from being the planner's primary path —
> you get sequential or partial-scan + ORDER BY, exactly the opposite of HNSW's purpose.
> At any non-trivial KB size, query latency explodes. The two-pass CTE keeps the ORDER BY
> path index-friendly and applies the score floor post-fetch with a generous buffer.

> **Per-query recall knob:** `hnsw.ef_search` defaults to 100 (good recall/latency tradeoff).
> Clients can request larger values for higher-recall queries; capped at 500 server-side.

---

### Component 5 — Ingestion Status Endpoint ⚡

```
GET /v1/knowledge-bases/{kb_id}/status
Response:
{
  "kb_id":           "<uuid>",
  "document_count":  42,
  "chunk_count":     1250,
  "pending_docs":    3,
  "failed_docs":     0,
  "last_updated_at": "2026-05-22T10:00:00Z"
}

GET    /v1/knowledge-bases/{kb_id}/documents               List documents (paginated)   ⚡
DELETE /v1/knowledge-bases/{kb_id}/documents/{doc_id}      Delete document              ⚡
```

> **Document delete — full cascade (MANDATORY for GDPR right-to-erasure):**
> ```
> Order of operations (each step idempotent):
> 1. DB transaction:
>      DELETE FROM rag.documents WHERE doc_id = $1;     -- cascades chunks + vector rows
>      INSERT INTO rag.s3_deletions (doc_id, tenant_id, s3_prefix, requested_at)
>        VALUES ($1, $2, '<tenant_id>/<doc_id>/', NOW());
>    COMMIT;
> 2. Async S3 sweeper (every 30s, batch 200): `DeleteObjects` under each
>    s3_prefix; on success, DELETE the s3_deletions row. On failure, leave the
>    row (retried next tick). After 24h of failed retries, alertmanager fires
>    `rag_s3_deletion_stuck`.
> 3. The 7-day orphan sweep (Component 2 lifecycle) is the safety net for any
>    leaked prefix, NOT the primary delete path.
> ```
> Why a queue table instead of inline S3 call: a transient S3 outage at delete time
> must NOT block the DB delete (the user requested erasure; we owe them that
> commitment), but the S3 object must still go. The queue is the durable handoff.

```sql
CREATE TABLE rag.s3_deletions (
  doc_id        UUID PRIMARY KEY,           -- one row per deleted document
  tenant_id     UUID         NOT NULL,
  s3_prefix     TEXT         NOT NULL,      -- e.g., "<tenant_id>/<doc_id>/"
  requested_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX idx_s3_deletions_pending ON rag.s3_deletions (requested_at) WHERE attempts < 100;
-- Platform-internal table — no RLS (only rag-service writes/reads).
```

---

### Component 5b — Transactional Outbox ⚡

Same divergence risk as Phases 3/4: if the DB write succeeds but the Kafka publish fails (broker hiccup, network partition), downstream consumers (Skills, dashboards) silently miss `ingestion.completed` / `ingestion.failed` events. Phase 5 events aren't billing-critical, but Skills (Phase 8) consumes them to refresh its skill registry — a missed event means a skill goes invisible until the next full reconcile.

```sql
CREATE TABLE rag.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,        -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,        -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX idx_outbox_unpublished
  ON rag.outbox(created_at) WHERE published_at IS NULL;
-- Platform-internal table — no RLS (only rag-service writes; reader is the same service).
```

Write path (one transaction):
```
BEGIN;
  UPDATE rag.documents SET status = 'completed', completed_at = NOW() WHERE doc_id = $1;
  INSERT INTO rag.outbox (topic, partition_key, payload) VALUES
    ('cypherx.rag.ingestion.completed', tenant_id::text, <Contract 5 envelope JSON>);
COMMIT;
```

Publisher loop: one goroutine per pod, batch SELECT 100, publish with partition_key, mark `published_at`, exponential backoff on failure, DLQ to `<topic>.dlq` after 10 attempts. Nightly job deletes rows where `published_at < NOW() - INTERVAL '7 days'`.

Event topics produced via the outbox (matching Contract 5 first-cycle parity):
- `cypherx.rag.ingestion.completed` — payload `{doc_id, kb_id, tenant_id, chunk_count, duration_ms, request_id, trace_id}`
- `cypherx.rag.ingestion.failed`    — payload `{doc_id, kb_id, tenant_id, error_code, error_msg, attempts, request_id, trace_id}`

Both topics provisioned alongside `cypherx.rag.ingestion.requested` — first cycle via the idempotent `topics-init` compose job (`rpk topic create`, safe to re-run); Phase 5 Terraform is the cloud form (see Compose-Parity Runtime subsection).

> **`request_id` propagation across the async boundary:** `request_id` is captured at
> `/ingest/finalize` time (= inbound `X-Request-ID`), written into the
> `ingestion.requested` payload, carried through the worker, and emitted in
> `ingestion.completed` / `ingestion.failed`. This lets investigators join
> `rag.documents` ↔ `llms.usage_records` (the embedding calls fired from the worker
> SHOULD forward the same `X-Request-ID` to LLMs Gateway) on a single field. Same
> provenance rule as Phase 3/4 — the service NEVER mints `request_id` if the header
> is present; falls back + WARN otherwise.

---

### Component 5c — KB Access Control (Per-Principal ACL) ⚡ (NEW)

By default, any agent in tenant T can query any KB in T. For external SaaS use, this is wrong — a chat-app vendor whose product is a KB-per-end-user needs partition-by-user, not partition-by-tenant.

**Data Model:**
```sql
CREATE TABLE rag.kb_acls (
  kb_id           UUID NOT NULL REFERENCES rag.knowledge_bases(kb_id) ON DELETE CASCADE,
  tenant_id       UUID NOT NULL,
  principal_type  VARCHAR(20) NOT NULL,        -- 'agent' | 'api_key' | 'user' | 'role' | 'tenant'
  principal_id    TEXT NOT NULL,                -- agent_id / api_key_id / opaque user_id from JWT / role name
                                                -- principal_type='tenant' + principal_id='*' = open to whole tenant
  permissions     TEXT[] NOT NULL,              -- subset of {read, query, ingest, write, admin}
  created_by      UUID NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ,
  PRIMARY KEY (kb_id, principal_type, principal_id)
);
CREATE INDEX ix_kb_acls_principal ON rag.kb_acls (tenant_id, principal_type, principal_id);
ALTER TABLE rag.kb_acls ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_kb_acls_tenant ON rag.kb_acls
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

**API Endpoints:**
```
GET    /v1/knowledge-bases/{kb_id}/acls           List ACLs           [scope: rag:admin (kb)]
PUT    /v1/knowledge-bases/{kb_id}/acls           Replace all ACLs    [scope: rag:admin (kb)]
POST   /v1/knowledge-bases/{kb_id}/acls           Add one ACL row     [scope: rag:admin (kb)]
DELETE /v1/knowledge-bases/{kb_id}/acls/{id}      Remove one          [scope: rag:admin (kb)]
```

**Default behaviour on KB creation:**
- KB is created with a default `(principal_type='tenant', principal_id='*', permissions=['read','query','ingest','write','admin'])` ACL — backward-compatible: tenant-wide access unless restricted.
- Caller can pass `private: true` on KB creation to OMIT the default; only the creator (or explicit ACL adds) can access.

> **KB rows created OUTSIDE the API path get NO automatic default ACL.** The only first-cycle case is Component 10's platform-skills bootstrap (direct SQL): it MUST insert the default `(tenant,'*')` ACL row in the same transaction as the KB row. Enforcement has no API-layer fallback — a KB with zero `kb_acls` rows is readable by no one.

**Enforcement (every retrieval / ingest / mgmt call):**
1. Resolve calling principal from JWT (`agent_id`, `api_key_id`, optionally `user_id` claim — JWT carries the resolved opaque user_id if present from the caller's BFF).
2. Query `rag.kb_acls` for `(kb_id, principal_type IN {agent, api_key, user, role, tenant} matching caller's identity, principal_id matching caller's identity OR '*')`.
3. Required permission for the operation must be in `permissions[]`. Otherwise 403 `FORBIDDEN_KB`.

> An external chat-app vendor uses `principal_type='user'` ACLs with the end-user UUID carried in their JWT's `cypherx:user_id` claim. The vendor's BFF mints these JWTs from the customer's identity tokens. No `tenant=*` ACL is created on those KBs.

---

### Component 5d — Usage Metering (Contract 19) ⚡ (NEW)

Every billable RAG operation emits one event on `cypherx.rag.usage.recorded` via the outbox in the same transaction as the operation. Without this, RAG cannot be priced per-tenant.

**Events per operation:**

| Operation | `units` payload | Cost driver |
|-----------|-----------------|-------------|
| `/query` | `{ chunks_returned, vector_bytes_scanned, top_k, reranked }` | compute (vector search + optional rerank) |
| `/ingest` (per finalized doc) | `{ chunks_indexed, embedding_tokens_used, storage_bytes_added, multimodal_pages_ocr, multimodal_image_embeds }` | embedding LLM cost + OCR (Textract per page) + image-embed inference + storage |
| `/multimodal/ocr` | `{ pages, document_bytes }` | Textract per-page price |
| `/multimodal/image-embed` | `{ images, embedding_tokens }` | CLIP inference + LLM gateway cost |
| `/webhook/ingest` | `{ documents_received, documents_accepted, bytes_received }` | bytes + downstream embed/storage |

Cost attribution (AMENDED 2026-06 — see Amendment Log; Contract-14 single-owner rule):
- Usage events carry **units + `request_id` ONLY** — no cost fields, and **no live cross-schema join against LLMs `provider_pricing`** (that claim is deleted). Billing joins happen **downstream in the usage pipeline**, never in the RAG hot path.
- All RAG unit costs live in `rag.pricing` (admin-managed). Per-tenant overrides in `rag.tenant_pricing` allow custom contracts. The downstream rollup computes from those rows: `storage_cost = storage_bytes_added * storage_unit_cost_per_byte_per_month / 30 / 86400` (per-second; rolled up monthly), `ocr_cost = pages * ocr_unit_cost`, `query_cost = vector_bytes_scanned * vector_scan_unit_cost + (reranked ? rerank_unit_cost : 0)`.
- Embedding **provider** cost is never computed here: the LLMs gateway meters the embedding call itself (its own `cypherx.llms.usage.recorded` event under the same `request_id`), and the billing rollup **de-duplicates on `request_id`** so embedding spend is attributed exactly once (the normative rule shared with Phase 6).

Events NEVER sampled. Outbox guarantees no event lost. Same `request_id` propagation as Component 5b.

---

### Component 5e — Pluggable Vector Storage (Storage Provider Interface) 📋 (NEW design / ⚡ interface)

**The interface is ⚡ first-cycle; only the pgvector adapter is ⚡ implemented.** Other backends (Pinecone, Qdrant, Weaviate) ship in 📋. Designed up-front so enterprise customers can BYO storage without rewriting query layers.

**Per-tenant backend selection:**
```sql
CREATE TABLE rag.tenant_backends (
  tenant_id        UUID PRIMARY KEY,
  backend_type     VARCHAR(20) NOT NULL DEFAULT 'pgvector',
                   -- pgvector | pinecone | qdrant | weaviate | (future)
  connection_ref   TEXT,                            -- secretsmanager:<arn> for backends with credentials
  config           JSONB NOT NULL DEFAULT '{}',     -- per-backend config (index name, region, etc.)
  status           VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Adapter interface (the ⚡ piece — every backend implements this):**
```python
class IVectorStore(Protocol):
    def upsert(tenant_id: UUID, chunks: list[ChunkVector]) -> None: ...
    def search(tenant_id: UUID, embedding: list[float], top_k: int,
               filter: dict, dimension: int) -> list[ChunkHit]: ...
    def delete(tenant_id: UUID, chunk_ids: list[UUID]) -> None: ...
    def estimate_size(tenant_id: UUID, kb_id: UUID) -> StorageStats: ...
```

**First-cycle:** the adapter resolver returns `PgVectorAdapter` for every tenant. A MISSING `rag.tenant_backends` row is treated as `backend_type='pgvector'` and the row is write-through-created on first touch — there is NO bootstrap-tenant consumer seeding rows per tenant (deleted; see Amendment Log). The interface exists so adding Pinecone is a new adapter class + per-tenant table update, never a query-layer change.

**Why interface-first even though we ship pgvector only:** retrofitting an abstraction after the codebase has 50+ direct SQL queries is expensive and bug-prone; adding the interface at the start costs almost nothing and unblocks Pinecone/Qdrant in a future PR.

---

### Component 6 — Hybrid Search (BM25 + Dense) 📋

**What it is:** Combine dense vector search (semantic) with sparse keyword search (BM25) for higher recall.

```
Hybrid score = alpha * dense_score + (1 - alpha) * sparse_score
alpha: configurable per query (default: 0.7)

BM25 implementation: PostgreSQL full-text search (tsvector/tsquery)
or dedicated Elasticsearch/OpenSearch (future upgrade path)
```

---

### Component 7 — Re-ranking 📋

**What it is:** After retrieving top-k chunks, re-rank using a cross-encoder model for precision.

```
1. Retrieve top 20 chunks via vector search
2. Pass (query, chunk) pairs to cross-encoder model
3. Re-rank by cross-encoder score
4. Return top-k from re-ranked list
```

---

### Component 8 — Query Expansion 📋

**What it is:** Rephrase the query multiple ways, retrieve for each, combine results.

```
1. Original query: "refund policy enterprise"
2. LLM generates alternatives:
   - "enterprise plan money back guarantee"
   - "cancellation and refund for business customers"
3. Retrieve top-5 for each query variant
4. Merge and de-duplicate results
5. Re-rank merged set
6. Return top-k
```

---

### Component 9 — Multi-modal Support 📋

**What it is:** Extract text from images (OCR), embed images, support code-specific chunking.

```
OCR: Tesseract or AWS Textract (behind IStorageProvider)
Image embedding: CLIP model via LLMs gateway vision model
Code chunking: AST-based splitting per language (Python, JS, Go, etc.)
```

---

### Component 10 — Platform Skills KB Bootstrap ⚡

The Phase 5 checklist requires "Skills knowledge base seeded under platform tenant (for Phase 8)" but no component owned the work. This is it.

**Bootstrap procedure (rag-service background bootstrap loop — lazy with retry; the listener does NOT wait for it):**

```
The platform tenant (00000000-0000-0000-0000-000000000001 per Contract 13) is a well-known
UUID, NOT a row in any rag.* table. Do NOT attempt to "ensure tenant exists" — RAG does
not own tenant lifecycle (Auth does). Just use the constant.

Embedding model resolution (NO live-LLMs hard dependency — see Amendment Log):
  - Prefer LLMs gateway GET /v1/models when reachable.
  - Otherwise fall back to env-pinned EMBEDDING_MODEL_RESOLVED / EMBEDDING_DIM
    (required env vars), so the bootstrap can never deadlock on the LLMs soft-dep
    (circular cold start).

PSEUDO (runs in a background loop, retry with backoff until success):
  with rag-service's runtime DB connection (user: rag_user):
    BEGIN;
      SET LOCAL app.tenant_id = '00000000-0000-0000-0000-000000000001';  -- enables RLS write
      INSERT INTO rag.knowledge_bases
        (kb_id, tenant_id, name, description,
         chunking_strategy, chunk_size, chunk_overlap,
         embedding_model_alias, embedding_model_resolved, embedding_dim)
      VALUES
        (gen_random_uuid(),
         '00000000-0000-0000-0000-000000000001',
         'platform-skills',
         'Platform-managed skill library — populated by Skills service (Phase 8).',
         'sentence', 512, 50,
         'embed',
         <resolved per the rule above>,
         <resolved dim>)
      ON CONFLICT (tenant_id, name) DO NOTHING;

      -- SAME TRANSACTION (the enforcement Component 5c requires — without this row
      -- the KB is readable by NO ONE once ACLs ship):
      kb_id := SELECT kb_id FROM rag.knowledge_bases
               WHERE tenant_id = '00000000-0000-0000-0000-000000000001'
                 AND name = 'platform-skills';
      INSERT INTO rag.kb_acls
        (kb_id, tenant_id, principal_type, principal_id, permissions, created_by)
      VALUES
        (kb_id, '00000000-0000-0000-0000-000000000001', 'tenant', '*',
         ARRAY['read','query','ingest','write','admin'],
         '00000000-0000-0000-0000-000000000001')
      ON CONFLICT (kb_id, principal_type, principal_id) DO NOTHING;
    COMMIT;

  Idempotent: safe on every pod start. No DDL privilege needed (runs under rag_user
  with the tenant context set, so the standard RLS policy permits the INSERT).
  No need for a privileged bootstrap user.

Readiness: /readyz requires ONLY that the bootstrap loop is RUNNING — not that the
KB row already exists. Queries against platform-skills before the loop's first
success return the normal not-found path; the loop converges within its backoff.
```

**Cross-tenant read access pattern (the answer Phase 8 codes against):**

First-cycle KB RLS is single-tenant (`tenant_id = current`). Phase 8 needs to query the
`platform-skills` KB on behalf of agents in many tenants. The mechanism is **NOT** a
mixed-scope RLS policy on RAG — RAG stays single-tenant strict. Instead:

```
Skills service → RAG cross-tenant call (canonical pattern):

  1. An agent in tenant T invokes a skill. The skill's loader (in Skills service) needs
     content from platform-skills KB.

  2. Skills service mints a NEW service JWT (Contract 12) for THIS specific RAG call:
        sub:           "svc:skills-service"
        aud:           ["rag-service"]
        tenant_id:     "00000000-0000-0000-0000-000000000001"   ← platform tenant, NOT T
        on_behalf_of:  "<agent-uuid in T>"                       ← preserves audit trail
        scopes:        ["internal:read"]

  3. Skills calls RAG /v1/knowledge-bases/{platform_skills_kb_id}/query with that token.

  4. RAG's standard JWT-derived tenant handler does `SET LOCAL app.tenant_id =
     '00000000-...-0001'`. RLS naturally allows reads from the platform-skills KB.

  5. RAG returns chunks. Skills service splices them into the agent's context and
     returns to the agent (which is still operating in tenant T's overall flow).

Audit trail: every query carries on_behalf_of so violations / billing /
investigations can trace back to the originating agent in T, even though the
DB-level tenant context was the platform tenant for that one query.

This pattern is reusable for any future cross-tenant read against a platform-owned
resource. It is the ONLY supported way to cross tenants in first cycle.

ANTI-PATTERN — do NOT do this:
- xAgent or any other service mutating its own `app.tenant_id` mid-request to
  query the platform KB. The tenant context is set ONCE at request boundary from
  the JWT; mid-request swaps make audit logs lie and break RLS guarantees.
- A mixed-scope RLS policy on rag.knowledge_bases. This expands the cross-tenant
  read surface beyond platform-skills with no per-KB gate.
```

**First-cycle decision (locked):** the JWT-with-platform-tenant pattern above. 📋 follow-up
when tenants need to publish their own public KBs: add `is_public_read BOOLEAN` on
`knowledge_bases` AND a mixed-scope RLS policy gated on that flag. Until then, RAG RLS
stays strict single-tenant.

**Content ownership:** the contents of `platform-skills` are written by Phase 8 (Skills),
ingested into RAG using the same `/ingest/*` endpoints with the platform-tenant JWT.
Phase 5 only ensures the KB row exists so Phase 8 has a target.

---

### Compose-Parity Runtime (first cycle — AMENDED, see Amendment Log)

The first-cycle runtime is **docker compose + Neon (Postgres) + Valkey + Redpanda + MinIO**.
There is NO K8s, Kong, Istio, Doppler, AWS, or Argo in the first cycle. The K8s spec below is
the **deploy-target (cloud) form**, conditional on the infra phase. Compose equivalents:

- **Service:** one `rag-service` compose service; same image, env-driven config; `/livez` /
  `/readyz` wired as compose `healthcheck`s (startup grace via `start_period: 60s` — the
  startupProbe stand-in).
- **Object storage:** MinIO stands in for S3. `S3_BUCKET`, `S3_ENDPOINT`, and
  `S3_SSE_MODE ∈ {none,kms}` are fully env-driven (`none` against MinIO first cycle; `kms`
  re-enables the SSE-KMS pre-sign condition in the cloud form). Bucket created by an
  idempotent MinIO init job; IRSA is replaced by static MinIO credentials from env;
  lifecycle/orphan sweeps run as a scheduled script (CI schedule or cron sidecar).
- **External auth:** no Kong — the service verifies external agent JWTs DIRECTLY against
  Auth JWKS (`AUTH_JWKS_URL`); Kong's JWT plugin in front is the cloud form. The internal
  path (service JWT + `X-Forwarded-Agent-JWT`) is identical in both forms.
- **Kafka topics:** an idempotent `topics-init` compose job (`rpk topic create` against
  Redpanda, safe to re-run) provisions `cypherx.rag.ingestion.requested` / `.completed` /
  `.failed`, `cypherx.rag.usage.recorded` + DLQ pairs — the Terraform stand-in. Topic
  names/partitions identical.
- **Config/secrets:** every env var below is supplied via compose `.env` / environment
  blocks ("from Doppler" is the cloud form). `AUTH_*` / `LLMS_GATEWAY_URL` point at compose
  service DNS (e.g. `http://auth:8080`) instead of cluster DNS.
- **Scheduled jobs** (S3-deletion sweeper alerting, orphan sweep): in-process loop or cron
  sidecar first cycle; K8s CronJob in the cloud form.

### K8s Deployment Spec (deploy-target / cloud form — conditional on the infra phase)

```yaml
Namespace:   shared-core
Deployment:  rag-service
Replicas:    min 2, max 10 (HPA on CPU 70% — first-cycle minimum)
Node selector: node-role: core

Resources:
  requests: { cpu: 500m, memory: 768Mi }
  limits:   { cpu: 2000m, memory: 2Gi }

Startup probe (PG connection + pgvector extension load takes time on cold start):
  startupProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    failureThreshold: 12              # 60s grace

Health probes (Contract 7):
  livenessProbe:
    httpGet: { path: /livez, port: 8080 }
    periodSeconds: 10
    # Process-only — NEVER touches DB / S3 / LLMs / Kafka.
  readinessProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    # Hard deps (fail readiness):
    #   - PostgreSQL reachable
    #   - pgvector extension present (`SELECT 1 FROM pg_extension WHERE extname='vector'`)
    #   - S3 reachable (HEAD bucket)
    #   - Platform-skills KB bootstrap LOOP running (Component 10 — lazy-with-retry;
    #     KB-row existence is NOT a readiness gate, and no live LLMs call is required)
    # Soft deps (log + metric only):
    #   - Valkey (policy cache)
    #   - Kafka  (outbox keeps events durable until publisher reconnects)
    #   - LLMs gateway (only needed for ingestion, not query)

Env vars (env-driven — compose `.env` first cycle; Doppler-injected in the cloud form):
  DATABASE_URL                 (PgBouncer → rag schema, pgvector enabled, runtime user rag_user)
  VALKEY_URL                   (soft dep)
  KAFKA_BROKERS
  KAFKA_SASL_PASSWORD
  AUTH_SERVICE_URL             (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET     (Contract 12; from service-auth/rag-service/bootstrap_secret)
  LLMS_GATEWAY_URL             (http://llms-gateway.shared-core.svc.cluster.local:8080)
  S3_BUCKET                    (env-driven; cypherx-rag-<env> in the cloud form, MinIO bucket first cycle)
  S3_ENDPOINT                  (MinIO endpoint first cycle; unset/AWS default in the cloud form)
  S3_SSE_MODE                  (none | kms — 'none' first cycle against MinIO)
  AWS_REGION                   (cloud form only)
  RAG_S3_KMS_KEY_ID            (cloud form only — alias/cypherx-rag-<env>, used when S3_SSE_MODE=kms)
```

> **Service ACL (cross-phase Phase 2 update):** Phase 2's `auth.service_acl` seed
> must be extended with the following rows when Phase 5 deploys:
> - `rag-service → llms-gateway [internal:read, internal:write]` (embeddings)
> - `rag-service → auth-service [internal:read]` (service-token mint + JWKS)
>
> Phase 5's Atlas migration ships this as an idempotent `INSERT ... ON CONFLICT DO NOTHING`
> against `auth.service_acl` (allowed because the migration runs with platform-admin DDL credentials).
>
> **JWKS verification** follows the Phase 3 pattern: in-cluster URL only, 5-minute cache,
> refresh-on-`kid`-miss rate-limited to 1/min.

---

## ⚡ First Cycle Implementation Checklist

- [ ] Service architecture planned separately
- [ ] Knowledge base create/get/list endpoints — **alias resolved at creation; `embedding_model_resolved` + `embedding_dim` persisted immutably**
- [ ] **Counter columns dropped** (compute `document_count` / `chunk_count` on demand) — no race conditions
- [ ] **Object-storage bucket layout** — bucket fully env-driven (`S3_BUCKET`/`S3_ENDPOINT`), prefix `<tenant_id>/<doc_id>/<filename>`; first cycle = MinIO bucket via idempotent init job (compose parity — `cypherx-rag-<env>` + IRSA scoping + lifecycle policies are the cloud form)
- [ ] **Pre-signed URL with HARD policy conditions** (exact key from JWT-derived `tenant_id`, exact Content-Length, exact Content-Type; SSE-KMS condition applied only when `S3_SSE_MODE=kms` — cloud form; `none` against MinIO first cycle); size cap 100 MiB; content-type allowlist
- [ ] `/ingest/finalize` HeadObject's the expected key + tenant prefix before enqueueing
- [ ] Inline ingest endpoint with server-enforced 100 KiB cap (`VALIDATION_ERROR` on over-limit)
- [ ] **Kafka-backed ingestion queue** (`cypherx.rag.ingestion.requested`) + **DLQ pair** provisioned via the idempotent `topics-init` compose job (`rpk topic create`) first cycle — Phase 5 Terraform is the cloud form
- [ ] **`ingestion.requested` payload self-contained** (carries `embedding_model_resolved`, `embedding_dim`, `chunking_strategy`, `chunk_size`, `chunk_overlap`, `request_id`, `trace_id`); worker does NOT re-read KB config from DB
- [ ] **Idempotency-Key on `/ingest/finalize`** — Valkey-backed `rag-idemp:...`, 24h TTL, replay returns cached body with `Idempotent-Replay: true`; worker-side `(doc_id, content_sha256)` dedup as defence in depth
- [ ] **Embedding batch policy** in worker — ≤128 items AND ≤8 MiB per `/v1/embeddings` call; deterministic `Idempotency-Key: embed:{doc_id}:{batch_first_chunk_index}` per batch; forwards `X-Request-ID` + `traceparent`
- [ ] **Poison-pill / DLQ flow** — 3 attempts with backoff, then DLQ + `status=failed` + commit offset
- [ ] Fixed + sentence chunking strategies
- [ ] Embedding via LLMs Gateway (call /v1/embeddings, using service JWT + `X-Forwarded-Agent-JWT`)
- [ ] **pgvector storage with `chunks` metadata + per-dimension `chunk_vectors_<N>` tables, HNSW index**
- [ ] **GIN index** (`jsonb_path_ops`) on `chunks.metadata` for filter queries
- [ ] **RLS** enabled on `knowledge_bases`, `documents`, `chunks`, `chunk_vectors_<N>` (tenant isolation per Contract 13)
- [ ] **Two-pass CTE vector query** (HNSW-index-friendly); `hnsw.ef_search` per-query knob (default 100, cap 500)
- [ ] Query response uses `duration_ms` (not `latency_ms`) — cross-service consistency
- [ ] `top_k` server-capped at 100; over-cap → `VALIDATION_ERROR`
- [ ] Document delete (`DELETE /v1/knowledge-bases/{kb_id}/documents/{doc_id}`) — DB cascade for chunks + vectors; **`rag.s3_deletions` queue table** + 30s async sweeper for S3 object cleanup (NOT inline); stuck-sweep alert after 24h
- [ ] Document status tracking + **outbox-backed** Kafka events: `cypherx.rag.ingestion.completed` / `.failed`; **both payloads carry `request_id` + `trace_id`** for cross-service correlation
- [ ] **`rag.outbox` table + publisher loop + DLQ after 10 attempts** (no Kafka divergence)
- [ ] **Two auth paths documented and supported**: external (agent JWT verified DIRECTLY by the service against Auth JWKS — compose parity; Kong-fronted JWT verification is the cloud form) + internal (service JWT + X-Forwarded-Agent-JWT)
- [ ] **`AUTH_JWKS_URL` + `SERVICE_BOOTSTRAP_SECRET` env vars** (Phase 3 JWKS pattern)
- [ ] **Service ACL extension** seeded via migration (`rag-service → llms-gateway`, `rag-service → auth-service`)
- [ ] Atlas migrations (Contract 14) for `rag.*` schema (knowledge_bases, documents, chunks, chunk_vectors_<N>, outbox, s3_deletions, kb_acls, tenant_backends, pricing, tenant_pricing)
- [ ] **KB ACL (Component 5c) ⚡** — `rag.kb_acls` table with `principal_type IN {agent, api_key, user, role, tenant}`; default `tenant=*` ACL on KB creation; `private: true` opt-out; enforced on every query/ingest/mgmt call (403 `FORBIDDEN_KB`)
- [ ] **Usage metering (Component 5d) ⚡** — `cypherx.rag.usage.recorded` on every query, ingest, multimodal op via outbox; `rag.pricing` table with `embedding_cost`, `storage_cost`, `ocr_cost`, `query_cost`, `rerank_cost` knobs; per-tenant override table
- [ ] **Storage abstraction (Component 5e) ⚡ interface** — `IVectorStore` interface defined; `PgVectorAdapter` is the only impl in first cycle; missing `rag.tenant_backends` row resolves as `backend_type='pgvector'` with write-through on first touch (bootstrap-tenant consumer deleted — see Amendment Log)
- [ ] **`DELETE /v1/knowledge-bases/{kb_id}`** — promoted to ⚡ so external customers can self-serve KB delete on day one
- [ ] **Per-tenant storage quota enforcement** — `auth.tenant_quotas.rag.storage_bytes_max` checked at ingest write time (returns 413 `QUOTA_EXCEEDED`); `kbs_max`, `documents_per_kb_max`, `queries_per_min`, `ingest_jobs_per_hour` enforced via Valkey sliding window
- [ ] **`/livez`, `/readyz`, `/metrics`** endpoints; readiness gated on Postgres + pgvector + S3 + platform-skills bootstrap LOOP running (not KB-row existence; no live LLMs call)
- [ ] **Startup grace** configured (60s) so cold-start doesn't trip liveness — compose `healthcheck` `start_period: 60s` first cycle (K8s startupProbe is the cloud form)
- [ ] Runs as a compose service with `/livez` / `/readyz` wired as compose healthchecks (compose parity — see Compose-Parity Runtime subsection; deploy to K8s via ArgoCD is the cloud form, conditional on the infra phase)
- [ ] **Component 10: platform-skills KB bootstrapped via lazy-with-retry background loop** under tenant `00000000-0000-0000-0000-000000000001` via `SET LOCAL app.tenant_id` + `ON CONFLICT (tenant_id, name) DO NOTHING`; inserts the default `(tenant,'*')` `kb_acls` row in the SAME transaction; env-pinned `EMBEDDING_MODEL_RESOLVED`/`EMBEDDING_DIM` fallback (no live LLMs call required); uses runtime `rag_user` (no DDL escalation)
- [ ] **Cross-tenant read pattern documented** — Skills service mints platform-tenant service JWT with `on_behalf_of`; RAG RLS stays strict single-tenant; no mid-request `app.tenant_id` swaps anywhere in the platform

## 📋 Full Enterprise Implementation Checklist

- [ ] Hybrid search (BM25 + dense)
- [ ] Re-ranking with cross-encoder
- [ ] Query expansion
- [ ] All source types (URL scraping, JSON/CSV, S3, database, webhook)
- [ ] Semantic + recursive chunking strategies
- [ ] Incremental document updates (re-index only changed parts)
- [ ] **Document re-ingestion / update flow** (first cycle does DELETE + re-ingest; in-place update post first cycle)
- [ ] Document versioning
- [ ] Knowledge base access control per agent (mixed-scope RLS with `is_public_read` flag — alternative to Component 10's option (b))
- ("Per-tenant storage quotas" 📋 item DELETED 2026-06 — duplicate of the ⚡ quota-enforcement item above; quota ENFORCEMENT is single-owned by Phase 5 in the first cycle, and Phase 13 Domain 3 only TUNES limit values. See Amendment Log)
- [ ] Multi-modal: OCR + image embedding
- [ ] Code-specific chunking
- [ ] Query log for debugging
- [ ] Retrieval latency + hit rate metrics
- [ ] Upgrade path from pgvector → Qdrant documented and tested

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Operational Coupling Between Services — PARTIAL
Evidence: lines 13, 239–248. Single Kafka pipeline couples ingest→embed→storage.
**Mitigation (post-first-cycle):** introduce adapter pattern for vector storage and embedding providers; API-version `ingestion.requested` schema before splitting workers.

### 2. ACL Query-Time Scaling — REAL
Evidence: lines 451–475 (CTE filter), 589–631 (Component 5c). ACL enforced in app layer post-retrieval, not in vector search.
**Mitigation (Phase 6):** fold `principal_id` into WHERE as a tenant-indexed subquery to push ACL filtering into pgvector; eliminates in-memory post-fetch for KBs shared with 100+ principals.

### 3. Cross-Vector-Store Semantic Portability — PARTIAL
Evidence: lines 660–690 (Component 5e). `IVectorStore` covers upsert/search/delete/estimate only.
**Mitigation:** document portability constraint — metadata schema portable (JSONB → backend-native); embedding vectors tied to source backend's index flavor. Migration requires re-indexing under target backend's index config.

### 4. Embedding Amplification Abuse Protection — REAL
Evidence: line 934. Storage quota only; no embedding-token quota or concurrent-ingest limit.
**Mitigation:** add `auth.tenant_quotas.rag.embedding_tokens_per_month` (Valkey sliding-window counter, 429 on breach) and `ingest_jobs_concurrent_max` per tenant.

### 5. JSONB Metadata Filtering Scalability — ALREADY-ADDRESSED
Evidence: lines 362–363 (GIN index `jsonb_path_ops` already declared).

### 6. PostgreSQL Write Amplification — REAL
Evidence: lines 291–308. Per-chunk: 3 writes (chunks + vectors + outbox) without batching.
**Mitigation:** worker batches 100 chunks per multi-row INSERT (one to `rag.chunks`, one to `rag.chunk_vectors_<N>`) in a single transaction; one outbox row per document. Reduces round-trips from O(chunks) to O(chunks/100).

### 7. Event Schema Versioning & Replay Governance — PARTIAL
Evidence: lines 249–269, 572–576. No schema version on Kafka payloads.
**Mitigation:** add `schema_version: "1"` to `ingestion.requested`; on breaking changes bump and run dual-consumers for one release cycle; DLQ messages retained; replay only on explicit override or backfill job.

### 8. Platform Complexity Growth After Skills Phase — REAL
Evidence: lines 9, 11, 749–834. Skills/Memory/Tools all consume RAG without governance roadmap.
**Mitigation:** version query endpoints (`/v1/knowledge-bases/{kb_id}/query/v1`, `/v2`); per-service feature flags in `auth.service_permissions` for gradual rollout of new query semantics.
