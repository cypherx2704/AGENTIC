-- =====================================================================================
-- llms-gateway — WP06 BYOK (bring-your-own-key). PostgreSQL 16.
-- Idempotent: safe to re-run top-to-bottom.
--
-- Lets a tenant register their OWN provider API key(s) so their LLM traffic bills to
-- their upstream account instead of the platform key. The gateway resolves the
-- highest-priority active (or in-grace) tenant key for a provider at call time and
-- falls back to the platform key when none exists or BYOK is disabled.
--
-- Three tables:
--   llms.providers            — PLATFORM-SCOPED provider registry (no tenant_id, no RLS).
--   llms.secret_backends      — PLATFORM-SCOPED secret-backend registry (no RLS).
--   llms.tenant_provider_keys — TENANT-SCOPED BYOK keys (RLS on app.tenant_id).
--
-- SECRET HANDLING: tenant_provider_keys.secret_ref NEVER stores raw key material. It is
-- either an `env:NAME` reference (resolved from the process env) or a self-describing
-- `sealed:v1:<base64>` envelope (AES-256-GCM DEK, wrapped by the KEK from LLMS_BYOK_KEK).
-- Seal/unseal live in services/byok.py; the DB only ever sees the opaque reference.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() (standalone-safe; created in 0001)

CREATE SCHEMA IF NOT EXISTS llms;  -- standalone-safe (created in 0001)

-- =====================================================================================
-- PLATFORM-SCOPED TABLES (no tenant_id, no RLS — mirrors provider_pricing / rate_limits)
-- =====================================================================================

-- ── providers (registry) ──────────────────────────────────────────────────────────────
-- The canonical provider names a tenant BYOK key / alias may reference. PR-managed
-- (read-only at runtime). `default_priority` is a hint for ordering platform fallback
-- when several providers could serve a request (lower = preferred).
CREATE TABLE IF NOT EXISTS llms.providers (
  name             VARCHAR(50)  PRIMARY KEY,           -- openai | anthropic | mock | ...
  display_name     VARCHAR(100) NOT NULL,
  enabled          BOOLEAN      NOT NULL DEFAULT TRUE,
  default_priority INTEGER      NOT NULL DEFAULT 100,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

INSERT INTO llms.providers (name, display_name, enabled, default_priority) VALUES
  ('openai',    'OpenAI',    TRUE, 10),
  ('anthropic', 'Anthropic', TRUE, 20),
  ('mock',      'Mock',      TRUE, 1000)
ON CONFLICT (name) DO UPDATE SET
  display_name     = EXCLUDED.display_name,
  enabled          = EXCLUDED.enabled,
  default_priority = EXCLUDED.default_priority,
  updated_at       = NOW();

-- ── secret_backends (registry) ──────────────────────────────────────────────────────────
-- Where a secret physically lives + how to resolve it. `kind` selects the resolver in
-- services/byok.py: 'env' -> os.environ[config.var]; 'sealed' -> AES-GCM envelope unwrapped
-- with the KEK. PR-managed (read-only at runtime). The `config` JSONB is backend-specific
-- (e.g. the env var name, or the KEK env-var name + algorithm) and NEVER holds key material.
CREATE TABLE IF NOT EXISTS llms.secret_backends (
  name       VARCHAR(50) PRIMARY KEY,                  -- platform-env | platform-sealed | ...
  kind       VARCHAR(20) NOT NULL,                     -- env | sealed
  config     JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_secret_backends_kind CHECK (kind IN ('env', 'sealed'))
);

-- Seed one env backend (platform keys via ANTHROPIC_API_KEY / OPENAI_API_KEY env vars) and
-- one sealed backend (tenant BYOK keys wrapped by the KEK in LLMS_BYOK_KEK). The `config`
-- is descriptive only — the resolver reads the live env at call time, never the row.
INSERT INTO llms.secret_backends (name, kind, config) VALUES
  ('platform-env',    'env',    '{"description": "Platform provider keys read from the process env."}'::jsonb),
  ('platform-sealed', 'sealed', '{"algorithm": "AES-256-GCM", "kek_env": "LLMS_BYOK_KEK", "envelope_version": "v1"}'::jsonb)
ON CONFLICT (name) DO UPDATE SET
  kind       = EXCLUDED.kind,
  config     = EXCLUDED.config,
  updated_at = NOW();

-- =====================================================================================
-- TENANT-SCOPED TABLE (tenant_id + tenant-leading index + RLS)
-- =====================================================================================

-- ── tenant_provider_keys (BYOK) ─────────────────────────────────────────────────────────
-- One row per tenant BYOK key. `secret_ref` is an opaque reference (env:NAME OR
-- sealed:v1:<base64>) — NEVER raw key material. `status`:
--   active   : eligible for selection (highest priority wins).
--   rotating : the OLD key during a rotation; still eligible UNTIL grace_until passes
--              (so in-flight calls keyed to the old upstream key keep working).
--   revoked  : never selected.
-- `grace_until` is set only on a 'rotating' row; a 'rotating' row past its grace is
-- treated as expired and skipped by the resolver (see services/byok.resolve_provider_key).
CREATE TABLE IF NOT EXISTS llms.tenant_provider_keys (
  key_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID        NOT NULL,
  provider    VARCHAR(50) NOT NULL REFERENCES llms.providers (name),
  secret_ref  TEXT        NOT NULL,                    -- env:NAME | sealed:v1:<base64>
  priority    INTEGER     NOT NULL DEFAULT 100,        -- lower = preferred
  status      VARCHAR(20) NOT NULL DEFAULT 'active',   -- active | rotating | revoked
  grace_until TIMESTAMPTZ,                             -- set on a rotating (old) key only
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_tpk_status CHECK (status IN ('active', 'rotating', 'revoked'))
);

-- Selection index: the resolver reads (tenant_id, provider) ordered by priority, skipping
-- revoked keys. Partial (status != 'revoked') keeps the hot index small.
CREATE INDEX IF NOT EXISTS idx_tpk_tenant_provider_priority
  ON llms.tenant_provider_keys (tenant_id, provider, priority)
  WHERE status != 'revoked';

-- Keep updated_at fresh on UPDATE (rotation / revoke flip status + this).
CREATE OR REPLACE FUNCTION llms.tpk_touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tpk_touch_updated_at ON llms.tenant_provider_keys;
CREATE TRIGGER trg_tpk_touch_updated_at
  BEFORE UPDATE ON llms.tenant_provider_keys
  FOR EACH ROW EXECUTE FUNCTION llms.tpk_touch_updated_at();

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — tenant_provider_keys is strictly tenant-scoped.
-- Every query runs inside a tx that does SELECT set_config('app.tenant_id','<uuid>',true).
-- =====================================================================================

ALTER TABLE llms.tenant_provider_keys ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_tpk_tenant ON llms.tenant_provider_keys;
CREATE POLICY p_tpk_tenant ON llms.tenant_provider_keys FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- =====================================================================================
-- GRANTS to the runtime role (llms_user). RLS still applies on top of these.
-- =====================================================================================

-- providers / secret_backends: platform-scoped reference config, read-only at runtime.
GRANT SELECT ON llms.providers       TO llms_user;
GRANT SELECT ON llms.secret_backends TO llms_user;

-- tenant_provider_keys: app registers / rotates / revokes (UPDATE) / lists / revoke-by-delete.
GRANT SELECT, INSERT, UPDATE, DELETE ON llms.tenant_provider_keys TO llms_user;
