-- =====================================================================================
-- llms-gateway — first-cycle schema (Phase 3). PostgreSQL 16.
--
-- Run as a superuser / migration role. The `llms` schema is assumed to already exist
-- (created in Phase 1) but is created idempotently here so the file runs standalone.
-- Creates the first-cycle tables, indexes, Row Level Security (Contract 13), and the
-- grants the runtime role `llms_user` needs.
--
-- TENANT-SCOPED tables (tenant_id + tenant-leading index + RLS USING app.tenant_id):
--   usage_records, outbox
-- PLATFORM-SCOPED tables (no tenant_id, no RLS — provider_pricing):
--   provider_pricing
-- MIXED-SCOPE table (platform rows tenant_id IS NULL + per-tenant rows; RLS admits NULL):
--   model_aliases
--
-- The runtime role connects and runs every tenant-scoped query inside
--   BEGIN; SELECT set_config('app.tenant_id', '<uuid>', true); ...; COMMIT
-- (the Core in_tenant() helper). The runtime role is NOT a superuser and does NOT
-- BYPASSRLS, so RLS is enforced.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS llms;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'llms_user') THEN
    CREATE ROLE llms_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA llms TO llms_user;

-- =====================================================================================
-- PLATFORM-SCOPED TABLES (no RLS)
-- =====================================================================================

-- ── provider_pricing (Component 1) ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.provider_pricing (
  provider                          VARCHAR(50)   NOT NULL,
  model                             VARCHAR(100)  NOT NULL,
  input_cost_per_1k_tokens          NUMERIC(12,8) NOT NULL,
  output_cost_per_1k_tokens         NUMERIC(12,8) NOT NULL,
  cached_input_cost_per_1k_tokens   NUMERIC(12,8) NOT NULL DEFAULT 0,
  cache_creation_cost_per_1k_tokens NUMERIC(12,8) NOT NULL DEFAULT 0,
  currency                          CHAR(3)       NOT NULL DEFAULT 'USD',
  effective_from                    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  updated_at                        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  PRIMARY KEY (provider, model, effective_from)
);

-- =====================================================================================
-- MIXED-SCOPE TABLE (platform defaults + per-tenant overrides)
-- =====================================================================================

-- ── model_aliases (Component 2) ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.model_aliases (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID,                    -- NULL = platform default
  alias        VARCHAR(50)  NOT NULL,
  model_id     VARCHAR(100) NOT NULL,
  provider     VARCHAR(50)  NOT NULL,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_model_aliases_alias ON llms.model_aliases (alias);
-- NULL tenant_id is "distinct" under the UNIQUE constraint above, so a partial unique
-- index makes platform-default aliases (tenant_id IS NULL) genuinely unique by alias —
-- this is also the arbiter the seed's ON CONFLICT targets.
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_aliases_platform
  ON llms.model_aliases (alias) WHERE tenant_id IS NULL;

-- =====================================================================================
-- TENANT-SCOPED TABLES (tenant_id + tenant-leading index + RLS)
-- =====================================================================================

-- ── usage_records (Component 4) ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.usage_records (
  id                     UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id             UUID          NOT NULL,
  tenant_id              UUID          NOT NULL,
  agent_id               UUID,
  api_key_id             UUID,
  principal_type         VARCHAR(20)   NOT NULL,
  task_id                UUID,
  trace_id               UUID          NOT NULL,
  provider               VARCHAR(50)   NOT NULL,
  model                  VARCHAR(100)  NOT NULL,
  prompt_tokens          INTEGER       NOT NULL,
  completion_tokens      INTEGER       NOT NULL,
  total_tokens           INTEGER       NOT NULL,
  cached_prompt_tokens   INTEGER       NOT NULL DEFAULT 0,
  cache_creation_tokens  INTEGER       NOT NULL DEFAULT 0,
  cost_usd               NUMERIC(12,8) NOT NULL,
  duration_ms            INTEGER,
  status                 VARCHAR(20),
  created_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  -- Idempotent re-INSERT key for the billing-replay journal (Component 4).
  CONSTRAINT uq_usage_tenant_request UNIQUE (tenant_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_id  ON llms.usage_records (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_agent_id   ON llms.usage_records (agent_id, created_at DESC)
  WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_usage_api_key_id ON llms.usage_records (api_key_id, created_at DESC)
  WHERE api_key_id IS NOT NULL;

-- ── outbox (Component 4 — transactional outbox) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.outbox (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID,                          -- = partition_key; used for RLS
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,
  payload       JSONB        NOT NULL,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON llms.outbox (created_at) WHERE published_at IS NULL;

-- Keep outbox.tenant_id in sync with partition_key (which carries tenant_id) for RLS.
-- The application writes partition_key = tenant_id; backfill tenant_id from it on insert.
CREATE OR REPLACE FUNCTION llms.outbox_set_tenant() RETURNS trigger AS $$
BEGIN
  IF NEW.tenant_id IS NULL THEN
    NEW.tenant_id := NEW.partition_key::uuid;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_outbox_set_tenant ON llms.outbox;
CREATE TRIGGER trg_outbox_set_tenant
  BEFORE INSERT ON llms.outbox
  FOR EACH ROW EXECUTE FUNCTION llms.outbox_set_tenant();

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13)
-- Every tenant-scoped query runs inside a tx that does
--   SELECT set_config('app.tenant_id','<uuid>',true).
-- =====================================================================================

ALTER TABLE llms.usage_records ENABLE ROW LEVEL SECURITY;
-- outbox is an INTERNAL publish queue drained by a background task across ALL tenants;
-- tenant-RLS would block the drain (the publisher has no app.tenant_id set). Isolation is
-- in the payload, not the row. RLS intentionally NOT enabled on outbox.
ALTER TABLE llms.outbox        DISABLE ROW LEVEL SECURITY;
ALTER TABLE llms.model_aliases ENABLE ROW LEVEL SECURITY;

-- Tenant-scoped: usage_records.
DROP POLICY IF EXISTS p_usage_tenant ON llms.usage_records;
CREATE POLICY p_usage_tenant ON llms.usage_records FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- outbox: NO tenant policy (RLS disabled above — internal cross-tenant publish queue).
-- Drop any stale policy so re-applying the migration is idempotent.
DROP POLICY IF EXISTS p_outbox_tenant ON llms.outbox;

-- Mixed-scope: model_aliases — tenants READ platform defaults (tenant_id IS NULL) and
-- their own rows; they may only WRITE their own rows.
DROP POLICY IF EXISTS p_model_aliases_read ON llms.model_aliases;
CREATE POLICY p_model_aliases_read ON llms.model_aliases FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_model_aliases_write ON llms.model_aliases;
CREATE POLICY p_model_aliases_write ON llms.model_aliases FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- =====================================================================================
-- GRANTS to the runtime role (llms_user). RLS still applies on top of these.
-- =====================================================================================

-- usage_records: app inserts + reads; updates allowed for replay/idempotency.
GRANT SELECT, INSERT, UPDATE ON llms.usage_records TO llms_user;

-- outbox: app inserts; publisher reads + updates published_at/attempts.
GRANT SELECT, INSERT, UPDATE ON llms.outbox TO llms_user;

-- model_aliases: app reads platform + tenant rows; tenant-scoped writes.
GRANT SELECT, INSERT, UPDATE, DELETE ON llms.model_aliases TO llms_user;

-- provider_pricing: platform-scoped, read-only at runtime (PR-managed updates).
GRANT SELECT ON llms.provider_pricing TO llms_user;
