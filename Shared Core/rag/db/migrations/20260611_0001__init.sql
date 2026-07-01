-- =====================================================================================
-- rag-service — first-cycle schema (Phase 5 / WP09). PostgreSQL 16 + pgvector.
--
-- Run as a superuser / migration role. The `rag` schema + pgvector extension are created
-- idempotently here so the file runs standalone (the dev DB image is pgvector/pgvector:pg16).
-- Creates the first-cycle tables, indexes, Row Level Security (Contract 13), and the grants
-- the runtime role `rag_user` needs.
--
-- TENANT-SCOPED tables (tenant_id + tenant-leading index + RLS USING app.tenant_id):
--   knowledge_bases, documents, chunks, chunk_vectors_1536, kb_acls
-- PLATFORM-INTERNAL tables (no tenant_id, no RLS — only rag-service reads/writes):
--   outbox, s3_deletions, pricing, tenant_backends
--
-- Every tenant-scoped query runs inside
--   BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT
-- (the Core in_tenant() helper). `rag_user` is NOT a superuser and does NOT BYPASSRLS.
--
-- RLS policies use NULLIF(current_setting('app.tenant_id', true),'')::uuid so an unset /
-- pooled-reset tenant context yields NULL (admits no tenant rows) rather than throwing on
-- ''::uuid — the same posture proven across llms/guardrails/xagent.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector (vector type + HNSW)

CREATE SCHEMA IF NOT EXISTS rag;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_user') THEN
    CREATE ROLE rag_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA rag TO rag_user;

-- =====================================================================================
-- TENANT-SCOPED TABLES
-- =====================================================================================

-- ── knowledge_bases (Component 1) ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rag.knowledge_bases (
  kb_id                     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                 UUID         NOT NULL,
  name                      VARCHAR(255) NOT NULL,
  description               TEXT,
  chunking_strategy         VARCHAR(50)  NOT NULL DEFAULT 'sentence',  -- fixed | sentence (📋 semantic/recursive)
  chunk_size                INTEGER      NOT NULL DEFAULT 512,
  chunk_overlap             INTEGER      NOT NULL DEFAULT 50,
  embedding_model_alias     VARCHAR(100) NOT NULL DEFAULT 'embed',     -- requested alias
  embedding_model_resolved  VARCHAR(100) NOT NULL,                     -- literal model id, immutable post-create
  embedding_dim             INTEGER      NOT NULL,                     -- resolved at creation
  status                    VARCHAR(20)  NOT NULL DEFAULT 'active',
  created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, name)
);

-- ── documents (Component 2 — NO bucket-prefix CHECK; app-layer validation) ─────────────
CREATE TABLE IF NOT EXISTS rag.documents (
  doc_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  kb_id        UUID         NOT NULL REFERENCES rag.knowledge_bases(kb_id) ON DELETE CASCADE,
  tenant_id    UUID         NOT NULL,
  name         VARCHAR(500) NOT NULL,
  source_type  VARCHAR(50)  NOT NULL,   -- pdf | markdown | html | text | url
  source_uri   TEXT,                    -- s3://<S3_BUCKET env>/<tenant_id>/<doc_id>/<filename>
  status       VARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending | processing | completed | failed
  attempts     INTEGER      NOT NULL DEFAULT 0,
  error_msg    TEXT,
  metadata     JSONB        NOT NULL DEFAULT '{}',
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_documents_kb_id  ON rag.documents (kb_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON rag.documents (status);

-- ── chunks (Component 3 — dimension-agnostic metadata) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS rag.chunks (
  chunk_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id          UUID         NOT NULL REFERENCES rag.documents(doc_id) ON DELETE CASCADE,
  kb_id           UUID         NOT NULL REFERENCES rag.knowledge_bases(kb_id) ON DELETE CASCADE,
  tenant_id       UUID         NOT NULL,
  content         TEXT         NOT NULL,
  chunk_index     INTEGER      NOT NULL,
  embedding_model VARCHAR(100) NOT NULL,
  embedding_dim   INTEGER      NOT NULL,
  metadata        JSONB        NOT NULL DEFAULT '{}',   -- includes content_sha for worker dedup
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chunks_kb_id        ON rag.chunks (kb_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id       ON rag.chunks (doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_metadata_gin ON rag.chunks USING gin (metadata jsonb_path_ops);

-- ── chunk_vectors_1536 (per-dimension vector table + HNSW) ─────────────────────────────
CREATE TABLE IF NOT EXISTS rag.chunk_vectors_1536 (
  chunk_id  UUID         PRIMARY KEY REFERENCES rag.chunks(chunk_id) ON DELETE CASCADE,
  tenant_id UUID         NOT NULL,
  kb_id     UUID         NOT NULL,
  embedding vector(1536) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_vectors_1536_hnsw
  ON rag.chunk_vectors_1536 USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- ── kb_acls (Component 5c — per-principal access control) ──────────────────────────────
CREATE TABLE IF NOT EXISTS rag.kb_acls (
  kb_id           UUID NOT NULL REFERENCES rag.knowledge_bases(kb_id) ON DELETE CASCADE,
  tenant_id       UUID NOT NULL,
  principal_type  VARCHAR(20) NOT NULL,   -- 'agent' | 'api_key' | 'user' | 'role' | 'tenant'
  principal_id    TEXT NOT NULL,          -- principal_type='tenant' + principal_id='*' = whole tenant
  permissions     TEXT[] NOT NULL,        -- subset of {read, query, ingest, write, admin}
  created_by      UUID NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ,
  PRIMARY KEY (kb_id, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS ix_kb_acls_principal ON rag.kb_acls (tenant_id, principal_type, principal_id);

-- =====================================================================================
-- PLATFORM-INTERNAL TABLES (no RLS)
-- =====================================================================================

-- ── outbox (Component 5b — transactional outbox) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS rag.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,    -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,    -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON rag.outbox (created_at) WHERE published_at IS NULL;

-- ── s3_deletions (Component 5 — durable S3-delete handoff queue) ───────────────────────
CREATE TABLE IF NOT EXISTS rag.s3_deletions (
  doc_id        UUID PRIMARY KEY,
  tenant_id     UUID         NOT NULL,
  s3_prefix     TEXT         NOT NULL,   -- e.g. "<tenant_id>/<doc_id>/"
  requested_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_s3_deletions_pending ON rag.s3_deletions (requested_at) WHERE attempts < 100;

-- ── pricing (Component 5d — admin-managed RAG unit costs; units-only metering joins here) ─
CREATE TABLE IF NOT EXISTS rag.pricing (
  unit            VARCHAR(50)   PRIMARY KEY,  -- embedding_cost | storage_cost | ocr_cost | query_cost | rerank_cost
  unit_cost       NUMERIC(18,12) NOT NULL DEFAULT 0,
  currency        CHAR(3)       NOT NULL DEFAULT 'USD',
  updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ── tenant_backends (Component 5e — lazy pgvector default) ─────────────────────────────
CREATE TABLE IF NOT EXISTS rag.tenant_backends (
  tenant_id        UUID PRIMARY KEY,
  backend_type     VARCHAR(20) NOT NULL DEFAULT 'pgvector',  -- pgvector | pinecone | qdrant | weaviate (📋)
  connection_ref   TEXT,
  config           JSONB NOT NULL DEFAULT '{}',
  status           VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — strict single-tenant on the tenant-scoped tables.
-- =====================================================================================

ALTER TABLE rag.knowledge_bases    ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.documents          ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.chunks             ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.chunk_vectors_1536 ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag.kb_acls            ENABLE ROW LEVEL SECURITY;

-- Internal queues/config drained across ALL tenants by background tasks (no app.tenant_id):
ALTER TABLE rag.outbox          DISABLE ROW LEVEL SECURITY;
ALTER TABLE rag.s3_deletions    DISABLE ROW LEVEL SECURITY;
ALTER TABLE rag.tenant_backends DISABLE ROW LEVEL SECURITY;
ALTER TABLE rag.pricing         DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_kb_tenant ON rag.knowledge_bases;
CREATE POLICY p_kb_tenant ON rag.knowledge_bases FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_documents_tenant ON rag.documents;
CREATE POLICY p_documents_tenant ON rag.documents FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_chunks_tenant ON rag.chunks;
CREATE POLICY p_chunks_tenant ON rag.chunks FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_chunk_vectors_1536_tenant ON rag.chunk_vectors_1536;
CREATE POLICY p_chunk_vectors_1536_tenant ON rag.chunk_vectors_1536 FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_kb_acls_tenant ON rag.kb_acls;
CREATE POLICY p_kb_acls_tenant ON rag.kb_acls FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- =====================================================================================
-- GRANTS to the runtime role (rag_user). RLS still applies on top of these.
-- =====================================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON rag.knowledge_bases    TO rag_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON rag.documents          TO rag_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON rag.chunks             TO rag_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON rag.chunk_vectors_1536 TO rag_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON rag.kb_acls            TO rag_user;
GRANT SELECT, INSERT, UPDATE         ON rag.outbox             TO rag_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON rag.s3_deletions       TO rag_user;
GRANT SELECT, INSERT, UPDATE         ON rag.tenant_backends     TO rag_user;
GRANT SELECT                         ON rag.pricing             TO rag_user;
