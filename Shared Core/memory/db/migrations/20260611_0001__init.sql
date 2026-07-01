-- =====================================================================================
-- memory-service — first-cycle schema (Phase 6 / WP10). PostgreSQL 16 + pgvector.
--
-- Run as a superuser / migration role. Idempotent: safe to re-run top-to-bottom.
-- Creates the `memory` schema, the first-cycle tables, the pgvector HNSW index, Row
-- Level Security (Contract 13), and the grants the runtime role `mem_user` needs.
--
-- TENANT-SCOPED tables (tenant_id + tenant-leading index + RLS USING app.tenant_id):
--   tenant_config, memories, memory_vectors_1536, sessions, gdpr_wipe_log
-- PLATFORM-SCOPED table (no tenant_id, no RLS — plan limits):
--   pricing
-- INTERNAL publish queue (RLS disabled — drained cross-tenant by a background task):
--   outbox
--
-- Memory ownership is by PRINCIPAL (principal_type, principal_id) — an agent or an
-- end-user-on-behalf-of. The runtime role connects and runs every tenant-scoped query
-- inside  BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT  (the
-- in_tenant() helper). The runtime role is NOT a superuser and does NOT BYPASSRLS.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector (vector type + HNSW)

CREATE SCHEMA IF NOT EXISTS memory;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mem_user') THEN
    CREATE ROLE mem_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA memory TO mem_user;

-- =====================================================================================
-- PLATFORM-SCOPED TABLE (no RLS) — Contract-19 memory plan limits
-- =====================================================================================
CREATE TABLE IF NOT EXISTS memory.pricing (
  plan               VARCHAR(50)  PRIMARY KEY,
  memories_max       BIGINT       NOT NULL,
  storage_bytes_max  BIGINT       NOT NULL,
  stores_per_min     INTEGER      NOT NULL,
  retrieves_per_min  INTEGER      NOT NULL,
  updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Seed the three plan tiers (mirrors services/quota._FALLBACK_LIMITS).
INSERT INTO memory.pricing (plan, memories_max, storage_bytes_max, stores_per_min, retrieves_per_min)
VALUES
  ('free',           1000,         10485760,       60,    120),
  ('pro',            100000,       1073741824,     600,   1200),
  ('enterprise',     10000000,     1099511627776,  10000, 20000)
ON CONFLICT (plan) DO NOTHING;

-- =====================================================================================
-- TENANT-SCOPED TABLES
-- =====================================================================================

-- ── tenant_config — per-tenant visibility policy + dedup threshold ────────────────────
CREATE TABLE IF NOT EXISTS memory.tenant_config (
  tenant_id              UUID PRIMARY KEY,
  -- 'isolated' (DEFAULT): even tenant_shared memories stay with their owner.
  -- 'tenant'           : tenant_shared memories are visible tenant-wide.
  user_scope_visibility  VARCHAR(20)   NOT NULL DEFAULT 'isolated'
    CHECK (user_scope_visibility IN ('isolated', 'tenant')),
  dedup_threshold        NUMERIC(4,3)  NOT NULL DEFAULT 0.950,
  created_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ── memories — the principal-owned memory rows ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory.memories (
  id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID         NOT NULL,
  principal_type    VARCHAR(20)  NOT NULL,   -- 'agent' | 'user' | 'service'
  principal_id      VARCHAR(128) NOT NULL,   -- agent_id / user_id (text — not always a UUID)
  scope             VARCHAR(20)  NOT NULL DEFAULT 'principal_only'
    CHECK (scope IN ('principal_only', 'tenant_shared')),
  type              VARCHAR(64)  NOT NULL DEFAULT 'note',
  tags              TEXT[]       NOT NULL DEFAULT '{}',
  content           TEXT         NOT NULL,
  metadata          JSONB        NOT NULL DEFAULT '{}'::jsonb,
  session_id        VARCHAR(128),
  score             DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  last_accessed_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  expires_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memory.memories (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_principal
  ON memory.memories (tenant_id, principal_type, principal_id);
CREATE INDEX IF NOT EXISTS idx_memories_expires
  ON memory.memories (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_tags ON memory.memories USING GIN (tags);

-- ── memory_vectors_1536 — the embedding (1536-dim) + HNSW cosine index ────────────────
CREATE TABLE IF NOT EXISTS memory.memory_vectors_1536 (
  memory_id   UUID PRIMARY KEY REFERENCES memory.memories(id) ON DELETE CASCADE,
  tenant_id   UUID         NOT NULL,
  embedding   vector(1536) NOT NULL
);
-- HNSW index for fast approximate cosine-distance (<=>) search.
CREATE INDEX IF NOT EXISTS idx_memory_vectors_hnsw
  ON memory.memory_vectors_1536 USING hnsw (embedding vector_cosine_ops);

-- ── sessions — keyed by session_id, OWNED by a principal ──────────────────────────────
CREATE TABLE IF NOT EXISTS memory.sessions (
  session_id      VARCHAR(128) PRIMARY KEY,
  tenant_id       UUID         NOT NULL,
  principal_type  VARCHAR(20)  NOT NULL,
  principal_id    VARCHAR(128) NOT NULL,
  title           VARCHAR(256),
  metadata        JSONB        NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sessions_principal
  ON memory.sessions (tenant_id, principal_type, principal_id);

-- ── gdpr_wipe_log — the right-to-erasure audit trail ──────────────────────────────────
CREATE TABLE IF NOT EXISTS memory.gdpr_wipe_log (
  id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID         NOT NULL,
  principal_type  VARCHAR(20)  NOT NULL,
  principal_id    VARCHAR(128) NOT NULL,
  deleted_count   INTEGER      NOT NULL,
  reason          VARCHAR(512),
  requested_by    VARCHAR(160) NOT NULL,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_gdpr_wipe_tenant ON memory.gdpr_wipe_log (tenant_id, created_at DESC);

-- ── outbox (transactional outbox — internal cross-tenant publish queue) ───────────────
CREATE TABLE IF NOT EXISTS memory.outbox (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID,                          -- = partition_key; used for backfill only
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,
  payload       JSONB        NOT NULL,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
  ON memory.outbox (created_at) WHERE published_at IS NULL;

CREATE OR REPLACE FUNCTION memory.outbox_set_tenant() RETURNS trigger AS $$
BEGIN
  IF NEW.tenant_id IS NULL THEN
    NEW.tenant_id := NEW.partition_key::uuid;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_outbox_set_tenant ON memory.outbox;
CREATE TRIGGER trg_outbox_set_tenant
  BEFORE INSERT ON memory.outbox
  FOR EACH ROW EXECUTE FUNCTION memory.outbox_set_tenant();

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13). Every tenant-scoped query runs inside a tx that does
--   SELECT set_config('app.tenant_id','<uuid>',true).
-- =====================================================================================
ALTER TABLE memory.tenant_config       ENABLE  ROW LEVEL SECURITY;
ALTER TABLE memory.memories            ENABLE  ROW LEVEL SECURITY;
ALTER TABLE memory.memory_vectors_1536 ENABLE  ROW LEVEL SECURITY;
ALTER TABLE memory.sessions            ENABLE  ROW LEVEL SECURITY;
ALTER TABLE memory.gdpr_wipe_log       ENABLE  ROW LEVEL SECURITY;
-- outbox is drained cross-tenant by a background task with no app.tenant_id set; tenant
-- RLS would block the drain. Isolation is in the payload, not the row.
ALTER TABLE memory.outbox              DISABLE ROW LEVEL SECURITY;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['tenant_config','memories','memory_vectors_1536','sessions','gdpr_wipe_log']
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS p_%1$s_tenant ON memory.%1$s', t);
    EXECUTE format(
      'CREATE POLICY p_%1$s_tenant ON memory.%1$s FOR ALL '
      'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
      'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
      t
    );
  END LOOP;
END
$$;

-- =====================================================================================
-- GRANTS to the runtime role (mem_user). RLS still applies on top of these.
-- =====================================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON memory.tenant_config       TO mem_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON memory.memories            TO mem_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON memory.memory_vectors_1536 TO mem_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON memory.sessions            TO mem_user;
GRANT SELECT, INSERT                 ON memory.gdpr_wipe_log        TO mem_user;
GRANT SELECT, INSERT, UPDATE         ON memory.outbox              TO mem_user;
GRANT SELECT                         ON memory.pricing            TO mem_user;
