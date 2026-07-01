# Data model & schema

> The `cypherx_a1` PostgreSQL schema: a tenant-scoped, bitemporal knowledge **graph** (entities + typed edges) plus ingestion landing, connector state, an extraction cost ledger, RAG-KB bindings, app-owned ACLs, and a Contract-5 outbox — all RLS-isolated under `app.tenant_id` and run by the non-superuser role `cxa1_user`. Vectors live in SharedCore RAG (this app stores only a `vector_ref`); copilot memory lives in SharedCore Memory. The graph never leaves this schema.

This document is the authoritative reference for the database layer of cypherx-a1 ("Autonomous Engineering Memory"). Every statement below is quoted from or directly grounded in the real migration `db/migrations/20260614_0001__init.sql` and the data-access modules under `src/cypherx_a1/db/`.

---

## 1. Ownership split — what lives here vs. SharedCore

cypherx-a1 is a **consuming app** (a peer of `xAgent/ax-1`), not a SharedCore service. Its database owns exactly one kind of data: the **engineering knowledge graph and its ingestion machinery**. Everything else is delegated to SharedCore via versioned `/v1` contracts.

| Concern | Owner | How cypherx-a1 references it |
|---------|-------|------------------------------|
| Knowledge **graph** (nodes + typed edges, bitemporal) | **cypherx-a1** (`cypherx_a1.entities`, `cypherx_a1.edges`) | first-class tables here |
| Raw landing / audit, connector config + secrets, sync cursors | **cypherx-a1** | first-class tables here |
| Extraction idempotency + LLM cost ledger | **cypherx-a1** (`cypherx_a1.extraction_jobs`) | `llm_call_id` is the gateway billing key — never rewritten |
| **Vectors / chunks / embeddings** | SharedCore **RAG** | `entities.vector_ref` JSONB `{kb_id, doc_id, chunk_id}`; `rag_kbs` binds logical KB → resolved `kb_id` + pinned model |
| Copilot conversational working memory | SharedCore **Memory** | per-principal, never in this DB |
| Provider access (extraction chat, copilot answers, embeddings) | SharedCore **llms-gateway** / **RAG** | `llm_call_id` recorded in `extraction_jobs` |
| Cross-service publish | platform Kafka (Contract 5) | `cypherx_a1.outbox` |

The header comment of the init migration states the rule plainly:

```sql
-- OWNERSHIP SPLIT (see docs/03-data-model-and-schema.md):
--   * The GRAPH + raw landing + connectors + extraction ledger live HERE (app-owned).
--   * VECTORS live in the SharedCore RAG service (this app stores only a vector_ref).
--   * Copilot conversational memory lives in the SharedCore Memory service.
```

**The graph never enters RAG and never enters Memory.** Putting it in RAG would burn embedding cost on structural data; putting it in Memory would leak it cross-principal. Both are explicit, locked decisions.

---

## 2. Schema, roles, extensions, and the frozen image

Everything lives in one schema, `cypherx_a1`, created idempotently by the migration role:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE SCHEMA IF NOT EXISTS cypherx_a1;
```

Two roles, by design:

| Role | Used by | Privileges |
|------|---------|------------|
| `cxa1_ddl` (migration role) | the compose `migrate` job / Atlas | superuser-equivalent for DDL; the **only** role that may `CREATE EXTENSION` and `CREATE SCHEMA` |
| `cxa1_user` (runtime role) | the running app | `LOGIN`, table-level `SELECT/INSERT/UPDATE/DELETE` grants, **NOT** a superuser, **does NOT** `BYPASSRLS`, **cannot** `CREATE EXTENSION` |

The runtime role is created idempotently inside the migration:

```sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cxa1_user') THEN
    CREATE ROLE cxa1_user LOGIN;
  END IF;
END
$$;
GRANT USAGE ON SCHEMA cypherx_a1 TO cxa1_user;
```

**Why `pgvector`/AGE are not created here:** the database runs the **frozen `pgvector/pgvector:pg16` image**, and the runtime role cannot `CREATE EXTENSION`. The graph is intentionally implemented as an **adjacency list + recursive CTEs** (see §6) behind a swappable `GraphRetriever` seam — **no Apache AGE**, no graph extension. `pgcrypto` (for `gen_random_uuid()`) is the only extension created, and it is created by the migration role only. Vector search is delegated entirely to SharedCore RAG; this DB stores no embeddings.

The role password is set out-of-band by `migrate.sh` (`cxa1_user` runtime password), not in the migration SQL.

---

## 3. Table inventory

Twelve tables. Eleven are **tenant-scoped** (have `tenant_id NOT NULL`, a tenant-leading index, and an `ENABLE + FORCE` RLS policy). One — `outbox` — is **platform-internal** with RLS deliberately disabled.

| Table | Purpose | Tenant-scoped? | PK |
|-------|---------|:---:|----|
| `entities` | knowledge-graph **nodes** (bitemporal, FTS, `vector_ref`) | ✅ | `entity_id` |
| `edges` | typed **relationships** (adjacency list, bitemporal) | ✅ | `edge_id` |
| `identities` | cross-tool alias → canonical person entity | ✅ | `identity_id` |
| `raw_events` | immutable landing / audit (idempotent) | ✅ | `raw_id` |
| `connectors` | per-tenant connector installs + non-secret config | ✅ | `connector_id` |
| `connector_secrets` | KMS/BYOK-sealed credentials (1:1 with connectors) | ✅ | `connector_id` |
| `sync_cursors` | resumable per-(tenant, connector, stream) position | ✅ | `(tenant_id, connector_id, stream)` |
| `extraction_jobs` | extraction idempotency + LLM cost ledger | ✅ | `(tenant_id, node_id, content_sha, extractor_version)` |
| `citations` | RAG chunk/doc → graph entity/edge provenance | ✅ | `citation_id` |
| `resource_acls` | per-repo / per-team read rules (the tenancy decision) | ✅ | `acl_id` |
| `rag_kbs` | resolved RAG KB bindings (embedding model pinned) | ✅ | `(tenant_id, logical_name)` |
| `outbox` | Contract-5 publish queue | ❌ (no RLS) | `id` |

---

## 4. `entities` — the knowledge-graph nodes (bitemporal)

The crown jewel. Adjacency-list nodes covering nine entity kinds, with a stored full-text-search vector and a delegated `vector_ref` into RAG.

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.entities (
  entity_id    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID         NOT NULL,
  kind         VARCHAR(20)  NOT NULL,
  source       VARCHAR(40)  NOT NULL,                 -- github | jira | slack | ...
  external_id  TEXT,                                  -- id in the source system
  natural_key  TEXT         NOT NULL,                 -- stable dedup key within (tenant,kind)
  title        TEXT,
  search_text  TEXT,                                  -- normalized text for keyword search
  body_ref     JSONB,                                 -- {bucket,key} S3 pointer for large bodies
  attrs        JSONB        NOT NULL DEFAULT '{}',
  vector_ref   JSONB,                                 -- {kb_id,doc_id,chunk_id} in RAG (delegated)
  content_sha  TEXT,
  valid_from   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  valid_to     TIMESTAMPTZ,                           -- NULL = current version (bitemporal)
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  fts          tsvector GENERATED ALWAYS AS
                 (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(search_text,''))) STORED,

  CONSTRAINT entities_kind_enum CHECK (kind IN
    ('person','service','repo','feature','decision','incident','pr','ticket','document'))
);
```

### Column notes

| Column | Notes |
|--------|-------|
| `kind` | constrained to the nine engineering node kinds via `entities_kind_enum`: `person, service, repo, feature, decision, incident, pr, ticket, document` |
| `source` | system of record (`github` first for the MVP) |
| `external_id` | the id in the source system (nullable; synthesized nodes may have none) |
| `natural_key` | the **stable dedup key** within `(tenant, kind)` — drives upsert; survives re-ingest so `entity_id` is stable |
| `search_text` | normalized text fed into `fts` |
| `body_ref` | `{bucket, key}` S3 pointer for large bodies (the body itself is not stored in Postgres) |
| `vector_ref` | `{kb_id, doc_id, chunk_id}` — the **only** link to the RAG corpus; set after RAG ingest via `graph_repo.set_vector_ref` |
| `content_sha` | content hash; gates re-extraction (a node is re-extracted only when its `content_sha` changes) |
| `valid_from` / `valid_to` | the bitemporal validity window; `valid_to IS NULL` ⇒ this is the **current** version (see §5) |
| `fts` | a **`GENERATED ALWAYS … STORED`** `tsvector` (see §4.2) |

### 4.1 Indexes

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_entities_natural_current
  ON cypherx_a1.entities (tenant_id, kind, natural_key) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_entities_tenant       ON cypherx_a1.entities (tenant_id);
CREATE INDEX IF NOT EXISTS idx_entities_tenant_kind  ON cypherx_a1.entities (tenant_id, kind);
CREATE INDEX IF NOT EXISTS idx_entities_fts          ON cypherx_a1.entities USING GIN (fts);
CREATE INDEX IF NOT EXISTS idx_entities_attrs        ON cypherx_a1.entities USING GIN (attrs);
```

- `uq_entities_natural_current` — the **partial unique index** (see §7), the upsert conflict target.
- `idx_entities_tenant` / `idx_entities_tenant_kind` — tenant-leading (RLS-friendly) lookups.
- `idx_entities_fts` — GIN over the FTS column for keyword retrieval.
- `idx_entities_attrs` — GIN over the JSONB `attrs` for containment (`@>`) predicates.

### 4.2 The FTS generated column

`fts` is computed **by the database**, never written by the app:

```sql
fts tsvector GENERATED ALWAYS AS
  (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(search_text,''))) STORED
```

Because it is `GENERATED ALWAYS … STORED`, every insert/update recomputes it transactionally from `title` + `search_text`; the GIN index `idx_entities_fts` stays consistent automatically. The keyword leg of hybrid retrieval queries it via `ts_rank(fts, plainto_tsquery('english', …))`, with an `OR natural_key = :q` escape hatch so an exact natural-key query (e.g. `who_owns("owner/name")`) resolves the repo node directly. From `graph_repo.find_entities`:

```sql
SELECT entity_id, kind, source, natural_key, title, attrs, vector_ref,
       ts_rank(fts, plainto_tsquery('english', %(q)s)) AS rank
  FROM cypherx_a1.entities
 WHERE valid_to IS NULL
   AND (fts @@ plainto_tsquery('english', %(q)s) OR natural_key = %(q)s)
```

This keyword/FTS leg is **app-side** on purpose: RAG ships dense-only in the first cycle, so cypherx-a1 owns keyword, RRF fusion, rerank, and query expansion itself.

---

## 5. The bitemporal model

Both `entities` and `edges` are **bitemporal** via a `valid_from` / `valid_to` validity window. The convention is uniform and load-bearing:

> **`valid_to IS NULL` means "this is the current version."** A non-NULL `valid_to` is a historical (superseded) version.

This yields three guarantees:

1. **Stable identity across re-ingest.** Re-ingesting the same source object upserts the **current** row (matched by the partial unique index, §7) in place, keeping the same `entity_id` — so existing edges and citations stay valid. `graph_repo.upsert_entity` documents this directly:
   > "re-ingesting the same node updates in place and keeps the same `entity_id` (edges + citations stay valid)."
2. **Non-destructive extraction.** Re-extraction supersedes prior edges rather than duplicating them. `graph_repo.supersede_extracted_edges` closes prior extracted edges before a fresh pass:

   ```sql
   UPDATE cypherx_a1.edges
      SET valid_to = NOW()
    WHERE src_entity_id = %s
      AND valid_to IS NULL
      AND extractor_version <> 'ingest'
      AND extractor_version <> %s
   ```
   Edges created at ingest time (`extractor_version = 'ingest'`) are never superseded by an extraction pass.
3. **Point-in-time history is preserved.** Closed rows remain queryable; "current" reads always filter `WHERE valid_to IS NULL`. Every read in `graph_repo` (neighbors, owners, impact, experts, keyword search) carries that filter on both endpoints (`e.valid_to IS NULL AND n.valid_to IS NULL`).

Edges have **no natural unique constraint**, so `upsert_edge` is a deterministic "supersede-in-place": it `UPDATE`s the matching current edge `(src, dst, rel, valid_to IS NULL)` if present, else `INSERT`s. This keeps re-ingest idempotent without a unique index on edges.

---

## 6. `edges` — typed bitemporal relationships (adjacency list)

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.edges (
  edge_id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID         NOT NULL,
  src_entity_id     UUID         NOT NULL,
  dst_entity_id     UUID         NOT NULL,
  rel               VARCHAR(20)  NOT NULL,
  confidence        NUMERIC(4,3) NOT NULL DEFAULT 1.000,
  extractor_version VARCHAR(20)  NOT NULL DEFAULT 'ingest',
  evidence_chunk_ids UUID[]      NOT NULL DEFAULT '{}',
  metadata          JSONB        NOT NULL DEFAULT '{}',
  valid_from        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  valid_to          TIMESTAMPTZ,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  CONSTRAINT edges_rel_enum CHECK (rel IN
    ('owns','authored','reviewed','depends_on','caused','resolved','mentions',
     'decided_in','deployed','expert_in','part_of'))
);
```

| Column | Notes |
|--------|-------|
| `src_entity_id` / `dst_entity_id` | directed adjacency-list endpoints (both `entity_id`s; **no FK** — see below) |
| `rel` | constrained to eleven relation types via `edges_rel_enum`: `owns, authored, reviewed, depends_on, caused, resolved, mentions, decided_in, deployed, expert_in, part_of` |
| `confidence` | `NUMERIC(4,3)`, default `1.000`; LLM-extracted edges carry the model's confidence |
| `extractor_version` | `'ingest'` for structurally-derived edges, an extractor tag for LLM-extracted ones; drives supersession |
| `evidence_chunk_ids` | `UUID[]` of RAG chunk ids that justify the edge — the provenance/citation trail |
| `metadata` | free-form JSONB |

**No foreign keys** are declared from `edges` to `entities`. This is deliberate: it keeps the graph a pure adjacency list that the `GraphRetriever` seam can swap behind, and avoids ordering constraints during high-throughput ingest. Referential integrity is enforced application-side (the normalizer guarantees both endpoints exist before an edge is written).

### 6.1 Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_edges_src ON cypherx_a1.edges (tenant_id, src_entity_id, rel);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON cypherx_a1.edges (tenant_id, dst_entity_id, rel);
CREATE INDEX IF NOT EXISTS idx_edges_current
  ON cypherx_a1.edges (tenant_id, src_entity_id, rel) WHERE valid_to IS NULL;
```

- `idx_edges_src` `(tenant, src, rel)` — forward traversal (out-neighbours by relation).
- `idx_edges_dst` `(tenant, dst, rel)` — reverse traversal (in-neighbours / blast radius).
- `idx_edges_current` — a **partial index** over the current slice `WHERE valid_to IS NULL`, so the hot path (traversing the live graph) ignores all historical rows.

### 6.2 Recursive-CTE traversal (the mandatory graph engine)

Traversal is plain SQL — adjacency list + recursive CTE — which the locked decisions require ("adjacency-list + recursive-CTE mandatory"). The reverse-`depends_on` **blast radius** in `graph_repo.impact_of` is the canonical example:

```sql
WITH RECURSIVE blast AS (
    SELECT e.src_entity_id AS entity_id, 1 AS depth
      FROM cypherx_a1.edges e
     WHERE e.dst_entity_id = %(eid)s AND e.rel = 'depends_on' AND e.valid_to IS NULL
    UNION
    SELECT e.src_entity_id, b.depth + 1
      FROM cypherx_a1.edges e
      JOIN blast b ON e.dst_entity_id = b.entity_id
     WHERE e.rel = 'depends_on' AND e.valid_to IS NULL AND b.depth < %(max_hops)s
)
SELECT n.entity_id, n.kind, n.natural_key, n.title, n.attrs, min(b.depth) AS depth
  FROM blast b
  JOIN cypherx_a1.entities n ON n.entity_id = b.entity_id AND n.valid_to IS NULL
 GROUP BY n.entity_id, n.kind, n.natural_key, n.title, n.attrs
 ORDER BY depth ASC
 LIMIT %(limit)s
```

Other graph reads in `graph_repo` — `neighbors` (one-hop typed, direction `out`/`in`/`both`), `owners_of` (ownership-ish relations `owns/authored/reviewed/expert_in`), `experts_on` (FTS topic → strongest authored signal) — are single-level joins over the same partial-current slice. Because traversal is ordinary SQL behind the `GraphRetriever` interface, the storage engine is swappable without touching the query surface.

---

## 7. The partial unique index (current-slice dedup)

Bitemporal tables can hold many versions of the same logical node, so a plain unique constraint on `(tenant_id, kind, natural_key)` would be wrong — it would forbid history. The dedup invariant ("one current row per logical node") is instead enforced with a **partial unique index over the current slice only**:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_entities_natural_current
  ON cypherx_a1.entities (tenant_id, kind, natural_key) WHERE valid_to IS NULL;
```

This index is the **`ON CONFLICT` target** of the entity upsert in `graph_repo.upsert_entity`, which merges into the current row in place:

```sql
INSERT INTO cypherx_a1.entities
    (tenant_id, kind, source, external_id, natural_key, title, search_text, attrs, content_sha)
VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, ...)
ON CONFLICT (tenant_id, kind, natural_key) WHERE valid_to IS NULL
DO UPDATE SET
    title = EXCLUDED.title,
    search_text = EXCLUDED.search_text,
    external_id = COALESCE(EXCLUDED.external_id, cypherx_a1.entities.external_id),
    attrs = cypherx_a1.entities.attrs || EXCLUDED.attrs,
    content_sha = EXCLUDED.content_sha
RETURNING entity_id
```

Note the **JSONB merge** semantics: `attrs = cypherx_a1.entities.attrs || EXCLUDED.attrs` accumulates attributes across re-ingests rather than clobbering them, and `external_id` is preserved via `COALESCE` if a later ingest omits it. `tenant_id` is **never** carried in the body — it is read from the RLS GUC via `NULLIF(current_setting('app.tenant_id', true), '')::uuid`, so the upsert physically cannot write a cross-tenant row.

The index `WHERE valid_to IS NULL` predicate means historical versions (with `valid_to` set) are exempt from the uniqueness rule — exactly what a bitemporal model needs.

---

## 8. Ingestion & ledger tables

### 8.1 `identities` — cross-tool alias resolution

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.identities (
  identity_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID        NOT NULL,
  person_entity_id UUID     NOT NULL,
  source        VARCHAR(40) NOT NULL,                 -- github | slack | jira | email
  handle        TEXT        NOT NULL,                 -- login / uid / account id / email
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_identities UNIQUE (tenant_id, source, handle)
);
CREATE INDEX IF NOT EXISTS idx_identities_tenant ON cypherx_a1.identities (tenant_id);
```

Maps a `(source, handle)` alias (a GitHub login, Slack uid, Jira account id, email) to a canonical `person` entity, so the same human across tools resolves to one node. The `uq_identities` constraint makes the mapping idempotent per `(tenant, source, handle)`.

### 8.2 `raw_events` — immutable idempotent landing

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.raw_events (
  raw_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID         NOT NULL,
  source       VARCHAR(40)  NOT NULL,
  external_id  TEXT         NOT NULL,
  record_type  VARCHAR(40)  NOT NULL,                 -- commit | pull_request | issue | message
  content_sha  TEXT         NOT NULL,
  body_ref     JSONB,                                 -- S3 pointer for the raw payload
  payload      JSONB,                                 -- small inline payloads
  received_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_raw_events UNIQUE (tenant_id, source, external_id, content_sha)
);
CREATE INDEX IF NOT EXISTS idx_raw_events_tenant ON cypherx_a1.raw_events (tenant_id, received_at DESC);
```

The immutable audit / replay surface. Landing is idempotent on `(tenant_id, source, external_id, content_sha)`; `ingest_repo.record_raw_event` uses `ON CONFLICT … DO NOTHING` and returns whether the row was newly inserted so the pipeline can skip re-processing a duplicate:

```sql
INSERT INTO cypherx_a1.raw_events
    (tenant_id, source, external_id, record_type, content_sha, payload)
VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s, %s)
ON CONFLICT (tenant_id, source, external_id, content_sha) DO NOTHING
```

The runtime role has only `SELECT, INSERT` on `raw_events` — it is append-only at runtime (no UPDATE/DELETE grant).

### 8.3 `connectors` + `connector_secrets`

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.connectors (
  connector_id UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID         NOT NULL,
  kind         VARCHAR(40)  NOT NULL,                 -- github | jira | slack | ...
  display_name VARCHAR(255) NOT NULL,
  config       JSONB        NOT NULL DEFAULT '{}',    -- non-secret config (org, repos, urls)
  status       VARCHAR(20)  NOT NULL DEFAULT 'active',
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT connectors_status_enum CHECK (status IN ('active','paused','error')),
  CONSTRAINT uq_connectors UNIQUE (tenant_id, kind, display_name)
);
CREATE INDEX IF NOT EXISTS idx_connectors_tenant ON cypherx_a1.connectors (tenant_id);

CREATE TABLE IF NOT EXISTS cypherx_a1.connector_secrets (
  connector_id UUID         PRIMARY KEY,              -- 1:1 with connectors
  tenant_id    UUID         NOT NULL,
  sealed_value TEXT         NOT NULL,                 -- "sealed:v1:<...>" or "env:<NAME>"
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  rotated_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_connector_secrets_tenant ON cypherx_a1.connector_secrets (tenant_id);
```

`connectors` holds **non-secret** install config (org, repos, urls). Secrets are split into a separate table, 1:1 by `connector_id`, and stored only as a **sealed envelope** — `sealed_value` is `"sealed:v1:<...>"` (KMS/BYOK envelope) or `"env:<NAME>"` (a reference to an env var for local/dev). No plaintext credential ever touches Postgres. `rotated_at` tracks rotation. `connectors.get_or_create_connector` upserts with a JSONB config merge (`config = cypherx_a1.connectors.config || EXCLUDED.config`).

### 8.4 `sync_cursors` — resumable sync position

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.sync_cursors (
  tenant_id    UUID         NOT NULL,
  connector_id UUID         NOT NULL,
  stream       VARCHAR(60)  NOT NULL,                 -- e.g. repo:owner/name:pulls
  cursor       TEXT,                                  -- opaque connector cursor
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, connector_id, stream)
);
```

A composite-PK upsert table (`set_cursor` does `ON CONFLICT (tenant_id, connector_id, stream) DO UPDATE`). The `cursor` is an opaque per-connector token; `stream` is a logical sub-stream like `repo:owner/name:pulls`, so each repo/object-type resumes independently.

### 8.5 `extraction_jobs` — idempotency + cost ledger

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.extraction_jobs (
  tenant_id         UUID        NOT NULL,
  node_id           UUID        NOT NULL,             -- entity the extraction ran over
  content_sha       TEXT        NOT NULL,
  extractor_version VARCHAR(20) NOT NULL,
  status            VARCHAR(20) NOT NULL DEFAULT 'completed',
  edges_extracted   INTEGER     NOT NULL DEFAULT 0,
  llm_call_id       TEXT,                             -- gateway billing key (Contract 19)
  cost_usd          NUMERIC(12,8) NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, node_id, content_sha, extractor_version),
  CONSTRAINT extraction_status_enum CHECK (status IN ('completed','failed','running'))
);
```

This table does double duty:

- **Idempotency.** The composite PK `(tenant_id, node_id, content_sha, extractor_version)` means a given `(node, content, extractor)` is extracted exactly once. `ingest_repo.extraction_job_done` checks `status = 'completed'`, and `list_unextracted_entities` finds current entities with a `content_sha` and **no** completed job at the current `extractor_version` (limited to extractable kinds `pr, ticket, incident, decision, document`) — that drives the extraction pass. Re-extraction happens only when `content_sha` changes.
- **Cost ledger.** `llm_call_id` is the **gateway billing key** (Contract 19) and `cost_usd NUMERIC(12,8)` records the metered cost. The platform invariant holds: **never rewrite gateway cost** — cypherx-a1 records the gateway's `llm_call_id` and the cost it reports; it does not recompute billing. The runtime role has `SELECT, INSERT, UPDATE` (status can advance `running → completed/failed`) but **no DELETE**.

### 8.6 `citations` — provenance (RAG → graph)

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.citations (
  citation_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID        NOT NULL,
  kb_id       TEXT        NOT NULL,
  doc_id      TEXT,
  chunk_id    TEXT,
  entity_id   UUID,
  edge_id     UUID,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_citations_tenant ON cypherx_a1.citations (tenant_id);
CREATE INDEX IF NOT EXISTS idx_citations_chunk  ON cypherx_a1.citations (tenant_id, chunk_id);
```

The bridge that makes the copilot **cited**: each RAG `(kb_id, doc_id, chunk_id)` maps back to the graph `entity_id` (or `edge_id`) it originated from. `doc_id` is the stable citation key — each `RagDoc` corresponds to exactly one graph node, recorded at ingest. The retrieval orchestrator joins citations back to current entities to attach provenance to copilot answers (`ingest_repo.entities_for_chunks` / `entities_for_docs`, both `JOIN … e.valid_to IS NULL`). The runtime role has `SELECT, INSERT, DELETE` (no UPDATE — citations are immutable facts, re-issued rather than edited).

### 8.7 `resource_acls` — the tenancy decision

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.resource_acls (
  acl_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID        NOT NULL,
  resource_type VARCHAR(20) NOT NULL,                 -- repo | team | service
  resource_key  TEXT        NOT NULL,                 -- e.g. "owner/name"
  principal_type VARCHAR(20) NOT NULL,                -- agent | user | role | tenant
  principal_id  TEXT        NOT NULL,                 -- id or '*'
  permission    VARCHAR(20) NOT NULL DEFAULT 'read',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT resource_acls_principal_enum CHECK (principal_type IN ('agent','user','role','tenant')),
  CONSTRAINT uq_resource_acls UNIQUE (tenant_id, resource_type, resource_key, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_acls_lookup
  ON cypherx_a1.resource_acls (tenant_id, resource_type, resource_key);
```

This table **is** the locked tenancy model: *one tenant per org, a shared graph, plus app-owned per-repo / per-team read rules.* SharedCore Auth never models repos or teams — that authorization is owned here. A row grants `principal_type/principal_id` a `permission` (default `read`) on a `resource_type/resource_key` (e.g. `repo` `"owner/name"`). `principal_id = '*'` is a wildcard (all principals in the tenant); `principal_type = 'tenant'` is a tenant-wide grant. The lookup index is keyed `(tenant, resource_type, resource_key)` for the read-filter hot path.

### 8.8 `rag_kbs` — pinned RAG bindings

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.rag_kbs (
  tenant_id              UUID        NOT NULL,
  logical_name           VARCHAR(60) NOT NULL,        -- eng-code | eng-conversations | ...
  kb_id                  TEXT        NOT NULL,
  embedding_model_resolved TEXT      NOT NULL,
  embedding_dim          INTEGER     NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, logical_name)
);
```

Binds a logical KB name (`eng-code`, `eng-conversations`, `eng-docs`, `eng-incidents`) to the **resolved** RAG `kb_id`, the **explicitly pinned** `embedding_model_resolved`, and its `embedding_dim`. This is where the "never the `embed` alias" rule is materialized: the model is resolved once at KB creation and frozen — a KB can never silently switch embedding models underneath the corpus. `set_rag_kb` uses `ON CONFLICT (tenant_id, logical_name) DO NOTHING` so the binding is immutable after first creation.

---

## 9. `outbox` — the platform-internal publish queue

```sql
CREATE TABLE IF NOT EXISTS cypherx_a1.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,                -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,                -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cxa1_outbox_unpublished
  ON cypherx_a1.outbox (created_at) WHERE published_at IS NULL;
```

The transactional-outbox pattern (Contract 5). A domain write and its event are committed in the **same** `in_tenant` transaction (see `db/outbox.py`), then a background relay drains unpublished rows. `partition_key` carries the `tenant_id` (Contract-5 partitioning), and `payload` is the full envelope ready to publish to `cypherx.cypherxa1.*` topics (paired `.dlq`). The partial index `idx_cxa1_outbox_unpublished … WHERE published_at IS NULL` makes draining cheap. `attempts` / `last_error` support at-least-once retry.

**`outbox` has NO RLS — by design.** The relay drains across **all** tenants and sets no `app.tenant_id`; tenant-RLS would block the drain. The migration is explicit:

```sql
-- outbox is an INTERNAL publish queue drained by a background task across ALL tenants;
-- tenant-RLS would block the drain (the publisher sets no app.tenant_id). Isolation is in
-- the payload, not the row. RLS intentionally NOT enabled on outbox.
ALTER TABLE cypherx_a1.outbox DISABLE ROW LEVEL SECURITY;
```

Isolation lives in the **payload** (each row already carries its tenant in `partition_key` + envelope), not in a row policy. The runtime role gets `SELECT, INSERT, UPDATE` (UPDATE to mark `published_at` / bump `attempts`) but no DELETE.

---

## 10. Row-Level Security (Contract 13)

Every tenant-scoped table gets RLS **enabled and FORCEd**, with an identical isolation policy, applied by one loop in the migration:

```sql
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'entities','edges','identities','raw_events','connectors','connector_secrets',
    'sync_cursors','extraction_jobs','citations','resource_acls','rag_kbs'
  ]
  LOOP
    EXECUTE format('ALTER TABLE cypherx_a1.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE cypherx_a1.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I_isolation ON cypherx_a1.%I', t, t);
    EXECUTE format(
      'CREATE POLICY %I_isolation ON cypherx_a1.%I FOR ALL '
      'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
      'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
      t, t
    );
  END LOOP;
END
$$;
```

Key properties:

- **`FORCE ROW LEVEL SECURITY`** — RLS applies even to the table owner, so nothing inside this DB can read across tenants.
- **The `NULLIF` guard** — `NULLIF(current_setting('app.tenant_id', true), '')::uuid`. The `true` second arg to `current_setting` means "don't error if the GUC is unset"; `NULLIF(..., '')` turns an unset/empty GUC into `NULL`, and `tenant_id = NULL` matches **no rows**. So a query that forgets to set the tenant context **silently returns nothing — it never errors and never leaks**. This is the platform-wide RLS safety convention.
- **Both `USING` and `WITH CHECK`** — reads *and* writes are constrained to the current tenant; an `INSERT`/`UPDATE` that would land a row in another tenant is rejected.
- The runtime role `cxa1_user` is **not** a superuser and does **not** have `BYPASSRLS`, so these policies are inescapable from the app.

### 10.1 How the tenant context is set — `in_tenant`

The app never embeds `tenant_id` in query bodies. Instead it runs every tenant-scoped query inside the `in_tenant` helper (`db/pool.py`), which opens one transaction and sets the GUC transaction-locally (equivalent to `SET LOCAL`, PgBouncer/transaction-pool safe):

```python
async def in_tenant[T](pool, tenant_id, fn):
    """Run fn(conn) inside one transaction with app.tenant_id set for RLS."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
        return await fn(conn)
```

The repos (`graph_repo`, `ingest_repo`) all assume they run on a connection **already inside** an `in_tenant` transaction and never set the GUC themselves. The write SQL pulls `tenant_id` straight from the GUC — `NULLIF(current_setting('app.tenant_id', true), '')::uuid` — so identity can never be spoofed via a request body.

---

## 11. Grants

RLS sits *on top of* explicit table grants; the runtime role gets exactly the verbs each table needs and no more:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.entities          TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.edges             TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.identities        TO cxa1_user;
GRANT SELECT, INSERT                 ON cypherx_a1.raw_events         TO cxa1_user;  -- append-only
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.connectors        TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.connector_secrets TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.sync_cursors      TO cxa1_user;
GRANT SELECT, INSERT, UPDATE         ON cypherx_a1.extraction_jobs    TO cxa1_user;  -- no DELETE (ledger)
GRANT SELECT, INSERT, DELETE         ON cypherx_a1.citations          TO cxa1_user;  -- no UPDATE
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.resource_acls     TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.rag_kbs           TO cxa1_user;
GRANT SELECT, INSERT, UPDATE         ON cypherx_a1.outbox             TO cxa1_user;  -- no DELETE
```

The three "append-only-ish" grants encode invariants in the privilege layer: `raw_events` (immutable landing — `SELECT, INSERT`), `extraction_jobs` (cost ledger — no DELETE), `outbox` (publish queue — no DELETE, marked published via UPDATE).

---

## 12. Cascade-on-tenant-delete

A tenant's entire footprint is removable in one stroke because every tenant-scoped table keys on `tenant_id` and has RLS. When the platform emits a tenant-deletion event, cypherx-a1 — which consumes **only** `cypherx.tenant.*` events — performs a tenant purge that runs inside that tenant's `in_tenant` context and deletes from the eleven tenant-scoped tables. Under the `app.tenant_id` GUC, a `DELETE` with no extra predicate is automatically scoped by the RLS `USING` clause to exactly that tenant's rows, so a single sweep removes:

`entities, edges, identities, raw_events, connectors, connector_secrets, sync_cursors, extraction_jobs, citations, resource_acls, rag_kbs`.

Because there are **no cross-table foreign keys**, ordering is irrelevant and the delete cannot wedge on referential constraints. The `outbox` is excluded (it has no RLS and is drained/aged separately). The corresponding RAG corpus and Memory data are purged by their **owning** SharedCore services on the same tenant-deletion signal — cypherx-a1 deletes only what it owns, which by design is just the graph and its ingestion state.

> Note: the purge is driven by the platform tenant-lifecycle event; cypherx-a1 consumes strictly `cypherx.tenant.*` (and emits/consumes its own `cypherx.cypherxa1.*`). It does not subscribe to other domains' topics, and it must not break Contract-15 cases 1–10.

---

## 13. Migrations & Atlas plan (Contract 14)

Versioned PostgreSQL 16 SQL under `db/migrations/`, applied by the platform compose `--profile migrate` job (which mounts the dir at `/migrations/cypherx-a1` and runs against the Neon **DIRECT** endpoint as `cxa1_ddl`), or locally via Atlas.

| File | What |
|------|------|
| `20260614_0001__init.sql` | schema `cypherx_a1`, runtime role `cxa1_user`, all 12 tables, indexes, RLS (Contract 13), grants. **Idempotent / re-runnable.** |
| `20260614_0002__seed.sql` | seed the `auth.service_acl` edges so cypherx-a1 may mint Contract-12 service tokens; **no tenant-scoped data** (connectors/ACLs/KB bindings are created per-tenant at runtime). |
| `schema.sql` | flattened end-state snapshot for Atlas **drift detection**. |
| `atlas.hcl` | Atlas env config (`local`, `ci`). |

**Two apply paths** (`atlas.hcl` and `README.md`):

```bash
# Atlas (preferred)
DATABASE_URL="postgres://cxa1_ddl:...@<DIRECT-neon-host>/cypherx_platform?search_path=cypherx_a1&sslmode=require" \
  atlas migrate apply --env local

# or raw psql (what the compose migrate job does)
psql "$MIGRATE_DATABASE_URL" -f 20260614_0001__init.sql
psql "$MIGRATE_DATABASE_URL" -f 20260614_0002__seed.sql
```

For the first cycle, **`20260614_0001__init.sql` is authoritative** and the migrate job applies the versioned files directly via psql (like the other platform services); Atlas is used for **diff/drift** in CI (`atlas migrate diff` / `atlas schema inspect` regenerate `schema.sql`). The DIRECT (session-mode) endpoint is required for the migrate job because DDL needs session-level state; app traffic uses the POOLED endpoint.

### 13.1 The seed — Contract-12 service ACLs

`20260614_0002__seed.sql` registers cypherx-a1's outbound service-call permissions in `auth.service_acl`, seeded **here** (not in SharedCore/auth) so auth stays untouched; the compose migrate job applies cypherx-a1 **after** auth, so the table already exists. It is guarded on the **canonical** columns `(caller_service, target_service, allowed_scopes)` — explicitly **not** the rag-seed's buggy `(source_service, scopes)` — and is idempotent via `ON CONFLICT … DO NOTHING`:

```sql
INSERT INTO auth.service_acl (caller_service, target_service, allowed_scopes) VALUES
  ('cypherx-a1', 'auth-service',       ARRAY['internal:read']),
  ('cypherx-a1', 'llms-gateway',       ARRAY['internal:read','internal:write']),
  ('cypherx-a1', 'guardrails-service', ARRAY['internal:read','internal:write']),
  ('cypherx-a1', 'rag-service',        ARRAY['internal:read','internal:write']),
  ('cypherx-a1', 'memory-service',     ARRAY['internal:read','internal:write'])
ON CONFLICT (caller_service, target_service) DO NOTHING;
```

`internal:read`/`internal:write` is the platform convention (same as the xagent edges): the target service maps `internal:read` → its read scope (e.g. `rag:query` / `mem:read`) and `internal:write` → its write scope (`rag:ingest` / `mem:write`).

### 13.2 Cross-team bootstrap notes

The schema + role land via this app's `db/migrations/*__init.sql` (the compose migrate job), with the `cxa1_user` runtime password set by `migrate.sh`. For the deps-only local stack, `cypherx_a1` was added to `infra/dev/local/seed/postgres-init.sql` and `infra/modules/postgres-bootstrap/main.tf` so the schema/role exist there too.

### 13.3 Adding a tenant-scoped table later

Any new tenant-scoped table must carry `tenant_id UUID NOT NULL`, a tenant-leading index, and the `ENABLE + FORCE` RLS policy `USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)` with the matching `WITH CHECK` — and a **cross-tenant-denial CI test** under `tests/` (per `db/migrations/README.md`). Schema changes are additive (write-once / version-forever); keep `schema.sql` in sync.

---

## 14. Quick reference — invariants

| Invariant | Where enforced |
|-----------|----------------|
| One current node per `(tenant, kind, natural_key)` | partial unique index `uq_entities_natural_current … WHERE valid_to IS NULL` |
| `valid_to IS NULL` ⇒ current version (bitemporal) | `entities`, `edges`; every read filters it |
| No cross-tenant read/write | `FORCE` RLS + `NULLIF(current_setting('app.tenant_id', true), '')::uuid` on all 11 tenant tables; runtime role lacks `BYPASSRLS` |
| Identity never in request body | upserts pull `tenant_id` from the GUC, not the payload |
| Idempotent landing | `uq_raw_events (tenant_id, source, external_id, content_sha)` + `ON CONFLICT DO NOTHING` |
| Extract once per `(node, content, extractor)` | `extraction_jobs` composite PK |
| Never rewrite gateway cost | `extraction_jobs.llm_call_id` + `cost_usd` recorded as reported (Contract 19) |
| Pinned embedding model | `rag_kbs.embedding_model_resolved` immutable (`ON CONFLICT … DO NOTHING`) |
| Vectors not in this DB | only `entities.vector_ref` JSONB pointer into RAG |
| Graph not in RAG/Memory | enforced by ownership split (§1) |
| Outbox drains all tenants | `outbox` RLS disabled; isolation in `partition_key`/payload |
| No graph extension | adjacency list + recursive CTEs; frozen `pgvector/pgvector:pg16`; runtime role cannot `CREATE EXTENSION` |
