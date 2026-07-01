-- =====================================================================================
-- guardrails-service — flattened end-state snapshot (init + seed). PostgreSQL 16.
--
-- Declarative source-of-truth for `atlas schema apply` / drift detection. It is the
-- concatenation of:
--   20260608_0001__init.sql  (schema, tables, indexes, RLS, grants)
--   20260608_0002__seed.sql  (11 rule rows + the one platform-default policy)
-- Keep this file in sync when adding a versioned migration.
-- =====================================================================================

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
  cost_usd             NUMERIC(12,8) NOT NULL DEFAULT 0,       -- per-rule-eval price (WP07, Contract 19.1)
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- A COALESCE() expression is NOT valid in a PRIMARY KEY/table constraint — use a unique
-- index over (rule_id, COALESCE(tenant_id, ZERO_UUID)) instead (matches init.sql so the
-- declarative snapshot applies cleanly and shows no phantom drift).
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
  previous_policy_id   UUID REFERENCES guardrails.policies(policy_id),  -- append-only chain (prior version)
  root_policy_id       UUID,                                   -- STABLE logical id across versions (WP07)
  stream_mode          VARCHAR(20)  NOT NULL DEFAULT 'buffer', -- buffer (only mode first cycle)
  fail_mode_override   VARCHAR(20),                            -- NULL = use each rule's default_fail_mode
  created_by           UUID,                                   -- principal agent_id, audit only
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
-- Append-only version chain (WP07): group versions by root; exactly one active per root.
CREATE INDEX IF NOT EXISTS idx_policies_root
  ON guardrails.policies (root_policy_id, version DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_policies_active_per_root
  ON guardrails.policies (root_policy_id)
  WHERE status = 'active';

-- =====================================================================================
-- agent_policies — per-agent policy assignment (Component 3). TENANT-SCOPED.
-- policy_id stores the ROOT id (WP07); the resolver joins to the active version by root
-- so an append-only edit auto-flips every assigned agent.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.agent_policies (
  agent_id     UUID NOT NULL,
  tenant_id    UUID NOT NULL,
  policy_id    UUID NOT NULL REFERENCES guardrails.policies(policy_id),
  PRIMARY KEY (agent_id, tenant_id)
);

-- =====================================================================================
-- policy_audit — append-only audit of policy state changes (WP07). TENANT-SCOPED.
-- One row per audited change: created | edited | assigned | fail_mode_override_changed.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS guardrails.policy_audit (
  id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID         NOT NULL,
  root_policy_id  UUID         NOT NULL,
  policy_id       UUID         NOT NULL,
  action          VARCHAR(40)  NOT NULL,
  actor_agent_id  UUID,
  request_id      UUID,
  trace_id        UUID,
  details         JSONB        NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_policy_audit_root
  ON guardrails.policy_audit (root_policy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_policy_audit_tenant
  ON guardrails.policy_audit (tenant_id, created_at DESC);

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
-- Pluggable key_ref scheme (WP07): 'env:' (dev/BYO), 'sealed:' (prod sealed-secret),
-- and the legacy 'secretsmanager:' alias. ``version`` is a per-tenant rotation counter.
CREATE TABLE IF NOT EXISTS guardrails.tenant_redaction_keys (
  tenant_id   UUID NOT NULL,
  key_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key_ref     TEXT NOT NULL
              CONSTRAINT tenant_redaction_keys_key_ref_check
              CHECK (key_ref LIKE 'env:%' OR key_ref LIKE 'sealed:%' OR key_ref LIKE 'secretsmanager:%'),
  status      VARCHAR(20) NOT NULL DEFAULT 'current'
              CHECK (status IN ('current', 'pending', 'retired')),
  version     INTEGER NOT NULL DEFAULT 1,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retired_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_tenant_redaction_current
  ON guardrails.tenant_redaction_keys (tenant_id)
  WHERE status = 'current';
-- Resolver covering index (current first, then newest in-grace retired) + retirement scan.
CREATE INDEX IF NOT EXISTS ix_tenant_redaction_resolve
  ON guardrails.tenant_redaction_keys (tenant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tenant_redaction_retired
  ON guardrails.tenant_redaction_keys (retired_at)
  WHERE status = 'retired';

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
ALTER TABLE guardrails.policy_audit          ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrails.violations            ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrails.tenant_redaction_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardrails.outbox                ENABLE ROW LEVEL SECURITY;

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

-- Tenant-scoped: policy_audit (append-only — no UPDATE/DELETE grant below).
DROP POLICY IF EXISTS p_policy_audit_tenant ON guardrails.policy_audit;
CREATE POLICY p_policy_audit_tenant ON guardrails.policy_audit FOR ALL
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

-- Tenant-scoped: outbox (partition_key/tenant_id is the tenant).
DROP POLICY IF EXISTS p_outbox_tenant ON guardrails.outbox;
CREATE POLICY p_outbox_tenant ON guardrails.outbox FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- =====================================================================================
-- GRANTS to the runtime role (grd_user). RLS still applies on top of these.
-- =====================================================================================

-- rules: app reads platform + tenant rows; tenant-scoped writes (Phase 4b custom rules).
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.rules TO grd_user;

-- policies: app reads platform + tenant rows; tenant-scoped writes (Phase 4b CRUD).
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.policies TO grd_user;

-- agent_policies: app reads + assigns.
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.agent_policies TO grd_user;

-- policy_audit: append-only — INSERT + SELECT only (no UPDATE/DELETE at runtime).
GRANT SELECT, INSERT ON guardrails.policy_audit TO grd_user;

-- violations: append-only — INSERT + SELECT only (no UPDATE/DELETE at runtime).
GRANT SELECT, INSERT ON guardrails.violations TO grd_user;

-- tenant_redaction_keys: app reads; rotation writes; retirement job DELETEs (WP07).
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.tenant_redaction_keys TO grd_user;

-- outbox: app inserts; publisher reads + updates published_at/attempts.
GRANT SELECT, INSERT, UPDATE ON guardrails.outbox TO grd_user;


-- =====================================================================================
-- guardrails-service — first-cycle seed (Phase 4). PostgreSQL 16.
--
-- Seeds:
--   * guardrails.rules    — the 11 first-cycle platform rule rows (tenant_id IS NULL).
--   * guardrails.policies — exactly ONE platform-default policy (tenant_id IS NULL,
--     is_default = true) whose `rules` JSONB enables all 11 rules at their default action.
--     The fixed policy_id matches PLATFORM_DEFAULT_POLICY_ID in services/policy_engine.py
--     so DB-resolved and built-in policies agree.
--
-- Idempotent: ON CONFLICT DO NOTHING so re-running is safe.
-- =====================================================================================

-- ── rules (platform; tenant_id IS NULL) ───────────────────────────────────────────────
INSERT INTO guardrails.rules
  (rule_id, tenant_id, version, default_action, default_fail_mode, default_stream_mode,
   default_severity, default_category, direction, timeout_ms, status)
VALUES
  -- INPUT rules
  ('prompt-injection-v1',       NULL, '1', 'block',  'closed', 'buffer', 'critical', 'security',  'input',  10, 'active'),
  ('pii-email-v1',              NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'input',  10, 'active'),
  ('pii-phone-v1',              NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'input',  10, 'active'),
  ('pii-credit-card-v1',        NULL, '1', 'block',  'closed', 'buffer', 'high',     'pii',       'input',  10, 'active'),
  ('jailbreak-v1',              NULL, '1', 'block',  'closed', 'buffer', 'critical', 'jailbreak', 'input',  10, 'active'),
  ('toxicity-v1',               NULL, '1', 'block',  'closed', 'buffer', 'high',     'toxicity',  'input',  50, 'active'),
  -- OUTPUT rules
  ('output-pii-email-v1',       NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'output', 10, 'active'),
  ('output-pii-credit-card-v1', NULL, '1', 'block',  'closed', 'buffer', 'high',     'pii',       'output', 10, 'active'),
  ('output-jailbreak-leak-v1',  NULL, '1', 'block',  'closed', 'buffer', 'high',     'jailbreak', 'output', 10, 'active'),
  ('output-toxicity-v1',        NULL, '1', 'block',  'closed', 'buffer', 'high',     'toxicity',  'output', 50, 'active'),
  ('output-max-length-v1',      NULL, '1', 'block',  'closed', 'buffer', 'low',      'length',    'output', 10, 'active')
ON CONFLICT (rule_id) WHERE tenant_id IS NULL DO NOTHING;

-- ── policies (platform default; tenant_id IS NULL, is_default = true) ───────────────────
INSERT INTO guardrails.policies
  (policy_id, root_policy_id, tenant_id, name, version, status, rules, is_default)
VALUES (
  '00000000-0000-0000-0000-0000000d0001',
  '00000000-0000-0000-0000-0000000d0001',
  NULL,
  'Platform Default Policy',
  1,
  'active',
  '[
    {"rule_id": "prompt-injection-v1",       "enabled": true, "action_override": null},
    {"rule_id": "pii-email-v1",              "enabled": true, "action_override": null},
    {"rule_id": "pii-phone-v1",              "enabled": true, "action_override": null},
    {"rule_id": "pii-credit-card-v1",        "enabled": true, "action_override": null},
    {"rule_id": "jailbreak-v1",              "enabled": true, "action_override": null},
    {"rule_id": "toxicity-v1",               "enabled": true, "action_override": null},
    {"rule_id": "output-pii-email-v1",       "enabled": true, "action_override": null},
    {"rule_id": "output-pii-credit-card-v1", "enabled": true, "action_override": null},
    {"rule_id": "output-jailbreak-leak-v1",  "enabled": true, "action_override": null},
    {"rule_id": "output-toxicity-v1",        "enabled": true, "action_override": null},
    {"rule_id": "output-max-length-v1",      "enabled": true, "action_override": null}
  ]'::jsonb,
  true
)
ON CONFLICT (policy_id) DO NOTHING;
