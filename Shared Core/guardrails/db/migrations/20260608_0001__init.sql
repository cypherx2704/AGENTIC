-- =====================================================================================
-- guardrails-service — first-cycle schema (Phase 4). PostgreSQL 16.
--
-- Run as a superuser / migration role. The `guardrails` schema is assumed to already
-- exist (created in Phase 1) but is created idempotently here so the file runs
-- standalone. Creates the first-cycle tables, indexes, Row Level Security (Contract 13),
-- and the grants the runtime role `grd_user` needs.
--
-- SCOPE MODEL (Contract 13):
--   PLATFORM-SCOPED rows readable by all tenants (tenant_id IS NULL):
--     rules     (MIXED: platform NULL + per-tenant custom rules — Component 8)
--   MIXED-SCOPE (read platform defaults + own; write own):
--     policies
--   TENANT-SCOPED (RLS USING app.tenant_id):
--     agent_policies, violations, tenant_redaction_keys, outbox
--
-- The runtime role connects and runs every tenant-scoped query inside
--   BEGIN; SELECT set_config('app.tenant_id', '<uuid>', true); ...; COMMIT
-- (the Core in_tenant() helper). The runtime role is NOT a superuser and does NOT
-- BYPASSRLS, so RLS is enforced.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS guardrails;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grd_user') THEN
    CREATE ROLE grd_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA guardrails TO grd_user;

-- =====================================================================================
-- rules — registry / source of truth for rule IDs (Component 2/3).
-- MIXED-SCOPE: platform rows (tenant_id IS NULL) are the 11 first-cycle rules; tenants
-- may later add custom rows (Component 8, Phase 4b). RLS admits NULL + own tenant.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.rules (
  rule_id              VARCHAR(100) NOT NULL,
  tenant_id            UUID,                                   -- NULL = platform rule
  version              VARCHAR(20)  NOT NULL,
  default_action       VARCHAR(20)  NOT NULL,                  -- allow | warn | redact | block
  default_fail_mode    VARCHAR(20)  NOT NULL DEFAULT 'closed', -- closed | open
  default_stream_mode  VARCHAR(20)  NOT NULL DEFAULT 'buffer', -- buffer (only mode first cycle)
  default_severity     VARCHAR(20)  NOT NULL,                  -- info | low | medium | high | critical
  default_category     VARCHAR(50)  NOT NULL,                  -- security | pii | toxicity | jailbreak | length
  direction            VARCHAR(10)  NOT NULL,                  -- input | output | both
  timeout_ms           INTEGER      NOT NULL DEFAULT 10,
  status               VARCHAR(20)  NOT NULL DEFAULT 'active', -- active | deprecated | retired
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- (rule_id, tenant_id) uniqueness with NULL = platform, expressed as a UNIQUE INDEX: a
-- COALESCE expression is NOT permitted in a PRIMARY KEY / table constraint, only in an index.
-- (Replaces the invalid `PRIMARY KEY (rule_id, COALESCE(...))`.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_rules_id_tenant
  ON guardrails.rules (rule_id, COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid));
-- Platform rule IDs are globally unique (the arbiter the seed's ON CONFLICT targets).
CREATE UNIQUE INDEX IF NOT EXISTS uq_rules_platform
  ON guardrails.rules (rule_id) WHERE tenant_id IS NULL;

-- =====================================================================================
-- policies — named rule sets (Component 3). MIXED-SCOPE.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.policies (
  policy_id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID,                                   -- NULL = platform default
  name                 VARCHAR(255) NOT NULL,
  version              INTEGER      NOT NULL DEFAULT 1,
  status               VARCHAR(20)  NOT NULL DEFAULT 'active', -- active | superseded | deprecated
  rules                JSONB        NOT NULL,                  -- array of rule configs (-> rules.rule_id)
  is_default           BOOLEAN      NOT NULL DEFAULT false,
  previous_policy_id   UUID REFERENCES guardrails.policies(policy_id),  -- append-only chain
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- Only one active default per tenant, and exactly one active platform default.
CREATE UNIQUE INDEX IF NOT EXISTS policies_one_tenant_default
  ON guardrails.policies(tenant_id)
  WHERE is_default = true AND tenant_id IS NOT NULL AND status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS policies_one_platform_default
  ON guardrails.policies((1))
  WHERE is_default = true AND tenant_id IS NULL AND status = 'active';

-- =====================================================================================
-- agent_policies — per-agent policy assignment (Component 3). TENANT-SCOPED.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.agent_policies (
  agent_id     UUID NOT NULL,
  tenant_id    UUID NOT NULL,
  policy_id    UUID NOT NULL REFERENCES guardrails.policies(policy_id),
  PRIMARY KEY (agent_id, tenant_id)
);

-- =====================================================================================
-- violations — append-only violation log (Component 4). TENANT-SCOPED.
-- PK is UUID (a global sequence leaks cross-tenant rates). request_id + trace_id NOT NULL.
-- matched_text holds ONLY the redaction token (PII) or <=64-char truncation (non-PII).
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.violations (
  id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  check_id     UUID         NOT NULL,
  request_id   UUID         NOT NULL,                  -- = inbound X-Request-ID
  tenant_id    UUID         NOT NULL,
  agent_id     UUID,
  task_id      UUID,                                    -- nullable: non-task callers
  trace_id     UUID         NOT NULL,                  -- parsed from traceparent (Contract 8)
  policy_id    UUID         NOT NULL,
  direction    VARCHAR(10)  NOT NULL,                  -- input | output
  decision     VARCHAR(10)  NOT NULL,                  -- allow | warn | redact | block
  rule_id      VARCHAR(100) NOT NULL,
  rule_name    VARCHAR(255) NOT NULL,
  severity     VARCHAR(20)  NOT NULL,
  category     VARCHAR(50)  NOT NULL,
  matched_text TEXT,                                    -- token (PII) / <=64-char truncation
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_violations_tenant_id  ON guardrails.violations(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_violations_agent_id   ON guardrails.violations(agent_id, created_at DESC)
  WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_violations_rule_id    ON guardrails.violations(rule_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_violations_request_id ON guardrails.violations(request_id);

-- =====================================================================================
-- tenant_redaction_keys — per-tenant BYO HMAC key references (Component 5). TENANT-SCOPED.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.tenant_redaction_keys (
  tenant_id   UUID NOT NULL,
  key_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key_ref     TEXT NOT NULL CHECK (key_ref LIKE 'secretsmanager:%'),
  status      VARCHAR(20) NOT NULL DEFAULT 'current'
              CHECK (status IN ('current', 'pending', 'retired')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retired_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_tenant_redaction_current
  ON guardrails.tenant_redaction_keys (tenant_id)
  WHERE status = 'current';

-- =====================================================================================
-- outbox — transactional outbox (Component 4). TENANT-SCOPED via partition_key=tenant_id.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID,                          -- = partition_key; used for RLS
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,         -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,         -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
  ON guardrails.outbox(created_at) WHERE published_at IS NULL;

-- Keep outbox.tenant_id in sync with partition_key (which carries tenant_id) for RLS.
CREATE OR REPLACE FUNCTION guardrails.outbox_set_tenant() RETURNS trigger AS $$
BEGIN
  IF NEW.tenant_id IS NULL THEN
    NEW.tenant_id := NEW.partition_key::uuid;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_outbox_set_tenant ON guardrails.outbox;
CREATE TRIGGER trg_outbox_set_tenant
  BEFORE INSERT ON guardrails.outbox
  FOR EACH ROW EXECUTE FUNCTION guardrails.outbox_set_tenant();

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13)
-- =====================================================================================

ALTER TABLE guardrails.rules                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrails.policies              ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrails.agent_policies        ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrails.violations            ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrails.tenant_redaction_keys ENABLE ROW LEVEL SECURITY;
-- outbox is an INTERNAL publish queue, drained by a background task across ALL tenants;
-- tenant isolation lives in the payload, not the row. Tenant-RLS would block the drain
-- (the publisher has no app.tenant_id set), so RLS is intentionally NOT enabled on outbox.
ALTER TABLE guardrails.outbox                DISABLE ROW LEVEL SECURITY;

-- Mixed-scope: rules — read platform (NULL) + own; write only own.
DROP POLICY IF EXISTS p_rules_read ON guardrails.rules;
CREATE POLICY p_rules_read ON guardrails.rules FOR SELECT
  USING (tenant_id IS NULL OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS p_rules_write ON guardrails.rules;
CREATE POLICY p_rules_write ON guardrails.rules FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Mixed-scope: policies — read platform defaults (NULL) + own; write only own.
DROP POLICY IF EXISTS p_policies_read ON guardrails.policies;
CREATE POLICY p_policies_read ON guardrails.policies FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid OR tenant_id IS NULL);
DROP POLICY IF EXISTS p_policies_write ON guardrails.policies;
CREATE POLICY p_policies_write ON guardrails.policies FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Tenant-scoped: agent_policies.
DROP POLICY IF EXISTS p_agent_policies_tenant ON guardrails.agent_policies;
CREATE POLICY p_agent_policies_tenant ON guardrails.agent_policies FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Tenant-scoped: violations (append-only-ish — no UPDATE/DELETE grant below).
DROP POLICY IF EXISTS p_violations_tenant ON guardrails.violations;
CREATE POLICY p_violations_tenant ON guardrails.violations FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Tenant-scoped: tenant_redaction_keys.
DROP POLICY IF EXISTS p_tenant_redaction_keys ON guardrails.tenant_redaction_keys;
CREATE POLICY p_tenant_redaction_keys ON guardrails.tenant_redaction_keys FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- outbox: NO tenant policy (RLS disabled above — internal cross-tenant publish queue).
-- Drop any stale policy so re-applying the migration is idempotent.
DROP POLICY IF EXISTS p_outbox_tenant ON guardrails.outbox;

-- =====================================================================================
-- GRANTS to the runtime role (grd_user). RLS still applies on top of these.
-- =====================================================================================

-- rules: app reads platform + tenant rows; tenant-scoped writes (Phase 4b custom rules).
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.rules TO grd_user;

-- policies: app reads platform + tenant rows; tenant-scoped writes (Phase 4b CRUD).
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.policies TO grd_user;

-- agent_policies: app reads + assigns.
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.agent_policies TO grd_user;

-- violations: append-only — INSERT + SELECT only (no UPDATE/DELETE at runtime).
GRANT SELECT, INSERT ON guardrails.violations TO grd_user;

-- tenant_redaction_keys: app reads; rotation writes (Phase 4b).
GRANT SELECT, INSERT, UPDATE ON guardrails.tenant_redaction_keys TO grd_user;

-- outbox: app inserts; publisher reads + updates published_at/attempts.
GRANT SELECT, INSERT, UPDATE ON guardrails.outbox TO grd_user;
