# rag-service migrations (Phase 5 / WP09)

PostgreSQL 16 **+ pgvector** (dev image `pgvector/pgvector:pg16`), Atlas convention (same
layout as auth/llms/guardrails).

## Files

| File | Purpose |
|------|---------|
| `20260611_0001__init.sql` | Schema `rag`, the `vector` + `pgcrypto` extensions, all first-cycle tables (incl. `chunk_vectors_1536` with an HNSW cosine index), RLS policies, and grants to `rag_user`. |
| `20260611_0002__seed.sql` | `rag.pricing` unit-cost knobs + the `auth.service_acl` edges (`rag-service → llms-gateway`, `rag-service → auth-service`), guarded so it is a no-op when the `auth` schema is absent. |
| `20260614_0003__hybrid_fts.sql` | **Additive** hybrid-search support: a `GENERATED ALWAYS` `content_tsv` tsvector column + GIN index on `rag.chunks` (folds the optional `metadata->>'context'` prefix at weight `B` over `content` at weight `A`). Powers the lexical leg of `search_mode=hybrid`/`sparse`; the default `dense` path is unchanged. Auto-maintained by Postgres (no app write, no backfill). |
| `schema.sql` | Flattened end-state snapshot (init + seed + hybrid_fts) — declarative source-of-truth for `atlas schema apply` / drift detection. |
| `atlas.hcl` | Atlas project config (`local` + `ci` envs). |

## Tables

- **`knowledge_bases`** (tenant-scoped, RLS) — KB config; `embedding_model_resolved` + `embedding_dim` are resolved at creation and immutable. `UNIQUE (tenant_id, name)`.
- **`documents`** (tenant-scoped, RLS) — one row per ingested document; status `pending|processing|completed|failed`, `attempts` for the poison-pill flow. NO bucket-prefix CHECK (app-layer validation).
- **`chunks`** (tenant-scoped, RLS) — dimension-agnostic chunk metadata; `metadata` JSONB carries `content_sha` (worker dedup) + a GIN `jsonb_path_ops` index for `@>` filters.
- **`chunk_vectors_1536`** (tenant-scoped, RLS) — `vector(1536)` + HNSW (`vector_cosine_ops`, m=16, ef_construction=64). One table per supported dimension.
- **`kb_acls`** (tenant-scoped, RLS) — per-principal ACL (`agent|api_key|user|role|tenant`); `(tenant,'*')` = whole-tenant; PK `(kb_id, principal_type, principal_id)`.
- **`outbox`** (platform-internal, no RLS) — transactional outbox drained to Kafka by the publisher (`ingestion.requested/.completed/.failed`, `usage.recorded`).
- **`s3_deletions`** (platform-internal, no RLS) — durable S3-delete handoff queue drained by the sweeper.
- **`pricing`** (platform-internal, no RLS) — admin-managed RAG unit-cost knobs (units-only metering joins these downstream).
- **`tenant_backends`** (platform-internal, no RLS) — per-tenant vector backend; missing row = `pgvector` (write-through on first touch).

## RLS

Strict single-tenant on all tenant-scoped tables: `tenant_id = NULLIF(current_setting('app.tenant_id', true),'')::uuid` (an unset / pooled-reset context yields NULL → admits no rows, never throws on `''::uuid`). Cross-tenant platform reads (Skills → `platform-skills` KB) use a service JWT minted with the **platform** tenant_id + `on_behalf_of` — RAG itself never relaxes RLS.

## Run

```bash
# Atlas
atlas migrate apply --env local

# Plain psql (top-to-bottom, as a superuser/migration role)
psql "$DATABASE_URL" -f 20260611_0001__init.sql
psql "$DATABASE_URL" -f 20260611_0002__seed.sql
```

The runtime role `rag_user` is created idempotently and is **not** a superuser / does **not**
bypass RLS. Every tenant-scoped query runs inside
`BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT` (the Core `in_tenant()`
helper).
