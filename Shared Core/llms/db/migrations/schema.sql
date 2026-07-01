-- =====================================================================================
-- llms-gateway — flattened end-state snapshot (init + seed). PostgreSQL 16.
--
-- This is the declarative source-of-truth for `atlas schema apply` / drift detection.
-- It is the concatenation of:
--   20260608_0001__init.sql  (schema, tables, indexes, RLS, grants)
--   20260608_0002__seed.sql  (provider_pricing + platform model_aliases)
--   20260610_0003__llm_call_id_and_capabilities.sql
--     (llm_call_id billing key + model_capabilities + code/vision alias seeds)
--   20260610_0004__llms_wp05_rate_limits.sql
--     (rate_limits per-plan-tier reference config + free/pro/enterprise seed)
--   20260610_0005__llms_embeddings.sql
--     (usage_records.operation column + embed alias + text-embedding-3-small
--      pricing/capability seeds — WP06 POST /v1/embeddings)
-- Keep this file in sync when adding a versioned migration.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS llms;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'llms_user') THEN
    CREATE ROLE llms_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA llms TO llms_user;

-- ── provider_pricing (platform-scoped) ────────────────────────────────────────────────
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

-- ── model_aliases (mixed-scope) ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.model_aliases (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID,
  alias        VARCHAR(50)  NOT NULL,
  model_id     VARCHAR(100) NOT NULL,
  provider     VARCHAR(50)  NOT NULL,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_model_aliases_alias ON llms.model_aliases (alias);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_aliases_platform
  ON llms.model_aliases (alias) WHERE tenant_id IS NULL;

-- ── model_capabilities (platform-scoped) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.model_capabilities (
  model_id           VARCHAR(100) PRIMARY KEY,
  provider           VARCHAR(50)  NOT NULL,
  max_tokens_cap     INTEGER      NOT NULL,
  context_window     INTEGER      NOT NULL,
  supports_vision    BOOLEAN      NOT NULL DEFAULT false,
  supports_tools     BOOLEAN      NOT NULL DEFAULT true,
  supports_streaming BOOLEAN      NOT NULL DEFAULT true,
  embedding_dim      INTEGER,
  updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── rate_limits (platform-scoped — WP05 per-plan-tier reference config) ─────────────────
CREATE TABLE IF NOT EXISTS llms.rate_limits (
  plan                      VARCHAR(20)   PRIMARY KEY,
  requests_per_min          INTEGER       NOT NULL,
  prompt_tokens_per_min     BIGINT        NOT NULL,
  completion_tokens_per_min BIGINT        NOT NULL,
  cost_usd_per_hour         NUMERIC(12,4) NOT NULL DEFAULT 0,
  cost_usd_per_day          NUMERIC(12,4) NOT NULL DEFAULT 0,
  cost_usd_per_month        NUMERIC(12,4) NOT NULL DEFAULT 0,
  updated_at                TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_rate_limits_plan CHECK (plan IN ('free', 'pro', 'enterprise'))
);

-- ── usage_records (tenant-scoped) ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.usage_records (
  id                     UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Gateway-minted, one fresh UUIDv4 per provider call — THE billing uniqueness key.
  llm_call_id            UUID          NOT NULL,
  -- = inbound X-Request-ID; NON-unique correlation column (one upstream request_id
  -- legitimately spans multiple LLM calls — Contract 8; both bill).
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
  -- WP06: kind of call this row bills ("chat" default | "embedding").
  operation              VARCHAR(20)   NOT NULL DEFAULT 'chat',
  created_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_usage_llm_call UNIQUE (tenant_id, llm_call_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_request_id ON llms.usage_records (request_id);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_id  ON llms.usage_records (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_agent_id   ON llms.usage_records (agent_id, created_at DESC)
  WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_usage_api_key_id ON llms.usage_records (api_key_id, created_at DESC)
  WHERE api_key_id IS NOT NULL;

-- ── outbox (tenant-scoped) ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.outbox (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID,
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,
  payload       JSONB        NOT NULL,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON llms.outbox (created_at) WHERE published_at IS NULL;

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

-- ── RLS ─────────────────────────────────────────────────────────────────────────────────
ALTER TABLE llms.usage_records ENABLE ROW LEVEL SECURITY;
-- outbox is an INTERNAL publish queue drained by a background task across ALL tenants;
-- tenant-RLS would block the drain (the publisher sets no app.tenant_id). Isolation is in
-- the payload, not the row. Matches init.sql (must stay DISABLED) — do not re-enable.
ALTER TABLE llms.outbox        DISABLE ROW LEVEL SECURITY;
ALTER TABLE llms.model_aliases ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_usage_tenant ON llms.usage_records;
CREATE POLICY p_usage_tenant ON llms.usage_records FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- outbox: NO tenant policy (RLS disabled above — internal cross-tenant publish queue).
-- Drop any stale policy so re-applying is idempotent; matches init.sql.
DROP POLICY IF EXISTS p_outbox_tenant ON llms.outbox;

DROP POLICY IF EXISTS p_model_aliases_read ON llms.model_aliases;
CREATE POLICY p_model_aliases_read ON llms.model_aliases FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_model_aliases_write ON llms.model_aliases;
CREATE POLICY p_model_aliases_write ON llms.model_aliases FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- ── grants ───────────────────────────────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE ON llms.usage_records TO llms_user;
GRANT SELECT, INSERT, UPDATE ON llms.outbox        TO llms_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON llms.model_aliases TO llms_user;
GRANT SELECT ON llms.provider_pricing TO llms_user;
GRANT SELECT ON llms.model_capabilities TO llms_user;
GRANT SELECT ON llms.rate_limits TO llms_user;

-- ── seed ─────────────────────────────────────────────────────────────────────────────────
INSERT INTO llms.provider_pricing
  (provider, model, input_cost_per_1k_tokens, output_cost_per_1k_tokens,
   cached_input_cost_per_1k_tokens, cache_creation_cost_per_1k_tokens, effective_from)
VALUES
  ('anthropic', 'claude-opus-4-8',   0.01500000, 0.07500000, 0.00150000, 0.01875000, '2026-06-08T00:00:00Z'),
  ('anthropic', 'claude-sonnet-4-6', 0.00300000, 0.01500000, 0.00030000, 0.00375000, '2026-06-08T00:00:00Z'),
  ('anthropic', 'claude-haiku-4-5',  0.00080000, 0.00400000, 0.00008000, 0.00100000, '2026-06-08T00:00:00Z'),
  ('openai',    'gpt-4o',            0.00500000, 0.01500000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z'),
  ('openai',    'gpt-4o-mini',       0.00015000, 0.00060000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z'),
  ('openai',    'text-embedding-3-small', 0.00002000, 0.00000000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z'),
  ('cypherx',   'rerank-mock-v1',    0.00000000, 0.00000000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z'),
  ('cypherx',   'classify-stub-v1',  0.00000000, 0.00000000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z')
ON CONFLICT (provider, model, effective_from) DO NOTHING;

INSERT INTO llms.model_aliases (tenant_id, alias, model_id, provider)
VALUES
  (NULL, 'fast',    'claude-haiku-4-5',  'anthropic'),
  (NULL, 'smart',   'claude-sonnet-4-6', 'anthropic'),
  (NULL, 'code',    'claude-sonnet-4-6', 'anthropic'),
  (NULL, 'vision',  'claude-sonnet-4-6', 'anthropic'),
  (NULL, 'default', 'claude-sonnet-4-6', 'anthropic'),
  (NULL, 'embed',   'text-embedding-3-small', 'openai'),
  (NULL, 'rerank-default', 'rerank-mock-v1',   'cypherx'),
  (NULL, 'safety-default', 'classify-stub-v1', 'cypherx')
ON CONFLICT (alias) WHERE tenant_id IS NULL DO NOTHING;

INSERT INTO llms.model_capabilities
  (model_id, provider, max_tokens_cap, context_window,
   supports_vision, supports_tools, supports_streaming, embedding_dim)
VALUES
  ('claude-opus-4-8',   'anthropic', 32000, 200000, true, true, true, NULL),
  ('claude-sonnet-4-6', 'anthropic',  8192, 200000, true, true, true, NULL),
  ('claude-haiku-4-5',  'anthropic',  8192, 200000, true, true, true, NULL),
  ('gpt-4o',            'openai',    16384, 128000, true, true, true, NULL),
  ('gpt-4o-mini',       'openai',    16384, 128000, true, true, true, NULL),
  ('text-embedding-3-small', 'openai', 1, 8191, false, false, false, 1536),
  ('rerank-mock-v1',    'cypherx',   1, 8192, false, false, false, NULL),
  ('classify-stub-v1',  'cypherx',   1, 8192, false, false, false, NULL)
ON CONFLICT (model_id) DO NOTHING;

INSERT INTO llms.rate_limits
  (plan, requests_per_min, prompt_tokens_per_min, completion_tokens_per_min,
   cost_usd_per_hour, cost_usd_per_day, cost_usd_per_month)
VALUES
  ('free',           60,    100000,    50000, 0, 0, 0),
  ('pro',           600,   2000000,  1000000, 0, 0, 0),
  ('enterprise',  10000, 100000000, 50000000, 0, 0, 0)
ON CONFLICT (plan) DO UPDATE SET
  requests_per_min          = EXCLUDED.requests_per_min,
  prompt_tokens_per_min     = EXCLUDED.prompt_tokens_per_min,
  completion_tokens_per_min = EXCLUDED.completion_tokens_per_min,
  cost_usd_per_hour         = EXCLUDED.cost_usd_per_hour,
  cost_usd_per_day          = EXCLUDED.cost_usd_per_day,
  cost_usd_per_month        = EXCLUDED.cost_usd_per_month,
  updated_at                = NOW();

-- ── 20260610_0007__llms_acls.sql (WP06 per-key ACLs — Contract-18) ────────────────────
-- Tenant-scoped per-API-key allow-lists. A key with NO rows is UNRESTRICTED (the default);
-- NULL array column = no restriction on that dimension. RLS identical to usage_records.
CREATE TABLE IF NOT EXISTS llms.api_key_acls (
  acl_id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID         NOT NULL,
  api_key_id          UUID         NOT NULL,
  allowed_models      TEXT[],
  allowed_providers   TEXT[],
  allowed_operations  TEXT[],
  created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_api_key_acls_tenant_key
  ON llms.api_key_acls (tenant_id, api_key_id);

ALTER TABLE llms.api_key_acls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_api_key_acls_tenant ON llms.api_key_acls;
CREATE POLICY p_api_key_acls_tenant ON llms.api_key_acls FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON llms.api_key_acls TO llms_user;
