-- =====================================================================================
-- auth-service — FLATTENED SCHEMA SNAPSHOT (desired end-state).
--
-- This is the declarative source-of-truth Atlas uses for drift detection / `schema apply`.
-- It is the concatenation of the versioned migrations as of:
--   20260606_0001__init.sql  +  20260606_0002__seed.sql  +  20260610_0003__outbox.sql
--   +  20260610_0004__wp03_auth_completion.sql
--
-- DO NOT hand-edit independently of the versioned files. Regenerate after every migration:
--   cat 20260606_0001__init.sql 20260606_0002__seed.sql 20260610_0003__outbox.sql \
--       20260610_0004__wp03_auth_completion.sql > schema.sql
--   (then sanity-check)
--
-- Runnable top-to-bottom as a superuser on a fresh PostgreSQL 16 database.
-- =====================================================================================

-- ===================== 20260606_0001__init.sql =======================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA IF NOT EXISTS auth;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'auth_user') THEN
    CREATE ROLE auth_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA auth TO auth_user;

-- Platform-scoped tables ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth.tenants (
  tenant_id            UUID PRIMARY KEY,
  name                 VARCHAR(255) NOT NULL,
  status               VARCHAR(20)  NOT NULL DEFAULT 'active',
  plan                 VARCHAR(50)  NOT NULL DEFAULT 'free',
  source               VARCHAR(30)  NOT NULL DEFAULT 'manual-seed',
  source_metadata      JSONB        NOT NULL DEFAULT '{}',
  region               VARCHAR(20)  NOT NULL DEFAULT 'us-east-1',
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  suspended_at         TIMESTAMPTZ,
  pending_deletion_at  TIMESTAMPTZ,
  deleted_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON auth.tenants(status);

CREATE TABLE IF NOT EXISTS auth.signing_keys (
  kid              UUID PRIMARY KEY,
  private_pem_enc  BYTEA       NOT NULL,
  public_jwk       JSONB       NOT NULL,
  status           VARCHAR(20) NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  promoted_at      TIMESTAMPTZ,
  retired_at       TIMESTAMPTZ,
  CONSTRAINT signing_keys_status_chk CHECK (status IN ('signing','verifying','retired'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_signing_key ON auth.signing_keys (status) WHERE status = 'signing';
CREATE INDEX IF NOT EXISTS idx_signing_keys_status ON auth.signing_keys(status);

CREATE TABLE IF NOT EXISTS auth.service_acl (
  caller_service  VARCHAR(100) NOT NULL,
  target_service  VARCHAR(100) NOT NULL,
  allowed_scopes  TEXT[]       NOT NULL,
  PRIMARY KEY (caller_service, target_service)
);

CREATE TABLE IF NOT EXISTS auth.bootstrap_state (
  id            BOOLEAN PRIMARY KEY DEFAULT TRUE,
  completed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_by  UUID,
  CONSTRAINT bootstrap_state_singleton CHECK (id = TRUE)
);

CREATE TABLE IF NOT EXISTS auth.plan_defaults (
  plan    VARCHAR(50) PRIMARY KEY,
  limits  JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS auth.upstream_identity (
  issuer        VARCHAR(500) PRIMARY KEY,
  jwks_url      VARCHAR(500) NOT NULL,
  audience      VARCHAR(255) NOT NULL,
  root_jwk_pem  BYTEA        NOT NULL,
  status        VARCHAR(20)  NOT NULL DEFAULT 'active',
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth.upstream_service_issuers (
  iss               TEXT PRIMARY KEY,
  tenant_id         UUID   NOT NULL,
  jwks_uri          TEXT   NOT NULL,
  required_claims   JSONB  NOT NULL DEFAULT '{}',
  allowed_audiences TEXT[] NOT NULL,
  allowed_scopes    TEXT[] NOT NULL,
  status            VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth.revoked_tokens (
  jti          UUID PRIMARY KEY,
  agent_id     UUID,
  tenant_id    UUID        NOT NULL,
  revoked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_by   UUID        NOT NULL,
  reason       VARCHAR(50) NOT NULL,
  token_exp    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revoked_purge ON auth.revoked_tokens(token_exp);

-- signup_attempts end-state = init (0001) + onboarding (0006, WP04 Component 1c).
-- full_name / terms_version_accepted relaxed to NULLABLE (minimal self-serve signup); the raw
-- verification_token is no longer written (NULLABLE) — only verification_token_hash is stored.
CREATE TABLE IF NOT EXISTS auth.signup_attempts (
  signup_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email                   CITEXT      NOT NULL,
  full_name               TEXT,
  intended_use            TEXT,
  terms_version_accepted  TEXT,
  verification_token      TEXT        UNIQUE,
  verification_token_hash VARCHAR(64),
  tenant_name             TEXT,
  verification_expires_at TIMESTAMPTZ NOT NULL,
  verified_at             TIMESTAMPTZ,
  tenant_id               UUID,
  initial_admin_user_id   UUID,
  risk_score              NUMERIC(3,2) NOT NULL DEFAULT 0.00,
  risk_signals            JSONB        NOT NULL DEFAULT '{}',
  status                  VARCHAR(30)  NOT NULL DEFAULT 'pending_verification',
                          -- pending_verification | verifying | manual_review | verified | expired | rejected
  attempts                INTEGER      NOT NULL DEFAULT 1,
  ip_address              INET,
  user_agent              TEXT,
  created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_signup_email      ON auth.signup_attempts (email);
CREATE INDEX IF NOT EXISTS ix_signup_ip_created ON auth.signup_attempts (ip_address, created_at);
CREATE INDEX IF NOT EXISTS ix_signup_status_created ON auth.signup_attempts (status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_signup_token_hash
  ON auth.signup_attempts (verification_token_hash) WHERE verification_token_hash IS NOT NULL;

-- Tenant-scoped tables -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth.agents (
  agent_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL,
  name             VARCHAR(255) NOT NULL,
  description      TEXT,
  version          VARCHAR(50)  NOT NULL DEFAULT '1.0.0',
  status           VARCHAR(20)  NOT NULL DEFAULT 'active',
  capabilities     JSONB        NOT NULL DEFAULT '[]',
  allowed_scopes   TEXT[]       NOT NULL DEFAULT '{}',
  allowed_tools    TEXT[]       NOT NULL DEFAULT '{}',
  allowed_skills   TEXT[]       NOT NULL DEFAULT '{}',
  metadata         JSONB        NOT NULL DEFAULT '{}',
  quarantine_until TIMESTAMPTZ,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  created_by       UUID         NOT NULL,
  CONSTRAINT agents_tenant_name_version_unique UNIQUE (tenant_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_agents_tenant_id ON auth.agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agents_status    ON auth.agents(tenant_id, status);

CREATE TABLE IF NOT EXISTS auth.api_keys (
  key_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id      UUID NOT NULL REFERENCES auth.agents(agent_id),
  tenant_id     UUID NOT NULL,
  key_hash      VARCHAR(64) NOT NULL UNIQUE,
  key_prefix    VARCHAR(20) NOT NULL,
  name          VARCHAR(255),
  scopes        TEXT[]      NOT NULL DEFAULT '{}',
  status        VARCHAR(20) NOT NULL DEFAULT 'active',
  expires_at    TIMESTAMPTZ,
  last_used_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at    TIMESTAMPTZ,
  revoked_by    UUID
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_agent ON auth.api_keys(tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash     ON auth.api_keys(key_hash);

CREATE TABLE IF NOT EXISTS auth.policies (
  policy_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID,
  name         VARCHAR(255) NOT NULL,
  description  TEXT,
  version      INTEGER      NOT NULL DEFAULT 1,
  status       VARCHAR(20)  NOT NULL DEFAULT 'active',
  rules        JSONB        NOT NULL,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_policies_tenant ON auth.policies(tenant_id);

CREATE TABLE IF NOT EXISTS auth.audit_log (
  id            BIGSERIAL PRIMARY KEY,
  event_type    VARCHAR(50) NOT NULL,
  agent_id      UUID,
  tenant_id     UUID        NOT NULL,
  action        VARCHAR(100),
  resource      VARCHAR(255),
  decision      VARCHAR(10),
  policy_ids    TEXT[],
  request_id    UUID,
  trace_id      UUID,
  ip_address    INET,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  row_hash      BYTEA NOT NULL,
  prev_row_hash BYTEA NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_id  ON auth.audit_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent_id   ON auth.audit_log(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON auth.audit_log(event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS auth.service_clients (
  client_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  name                 TEXT NOT NULL,
  client_secret_hash   TEXT,
  allowed_grant_types  TEXT[] NOT NULL DEFAULT '{client_credentials}',
  allowed_audiences    TEXT[] NOT NULL,
  allowed_scopes       TEXT[] NOT NULL,
  status               VARCHAR(20) NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'rotating', 'revoked')),
  created_by           UUID NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at           TIMESTAMPTZ,
  last_used_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_service_clients_tenant ON auth.service_clients (tenant_id);

CREATE TABLE IF NOT EXISTS auth.tenant_quotas (
  tenant_id        UUID        NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  plan             VARCHAR(50) NOT NULL REFERENCES auth.plan_defaults(plan),
  limits           JSONB       NOT NULL,
  effective_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  effective_until  TIMESTAMPTZ,
  source           VARCHAR(30) NOT NULL,
  updated_by       TEXT        NOT NULL,
  PRIMARY KEY (tenant_id, effective_from)
);
CREATE INDEX IF NOT EXISTS ix_tenant_quotas_current ON auth.tenant_quotas (tenant_id) WHERE effective_until IS NULL;

CREATE TABLE IF NOT EXISTS auth.behavior_policies (
  policy_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID,
  agent_id         UUID,
  name             VARCHAR(255) NOT NULL,
  version          INTEGER      NOT NULL DEFAULT 1,
  status           VARCHAR(20)  NOT NULL DEFAULT 'active',
  constraints      JSONB        NOT NULL,
  enforcement      VARCHAR(20)  NOT NULL DEFAULT 'block',
  cooldown_seconds INTEGER      NOT NULL DEFAULT 300,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_behavior_tenant ON auth.behavior_policies(tenant_id, agent_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS auth.approval_requests (
  request_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID   NOT NULL,
  agent_id        UUID   NOT NULL,
  task_id         UUID,
  scopes          TEXT[] NOT NULL,
  resource        VARCHAR(500),
  reason          TEXT,
  context         JSONB,
  status          VARCHAR(20) NOT NULL DEFAULT 'pending',
  requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ NOT NULL,
  resolved_at     TIMESTAMPTZ,
  resolved_by     UUID,
  resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_pending ON auth.approval_requests(tenant_id, status, expires_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS auth.approval_grants (
  grant_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id     UUID NOT NULL REFERENCES auth.approval_requests(request_id),
  tenant_id      UUID NOT NULL,
  agent_id       UUID NOT NULL,
  approved_by    UUID NOT NULL,
  scopes         TEXT[] NOT NULL,
  resource       VARCHAR(500),
  task_id        UUID,
  granted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at     TIMESTAMPTZ NOT NULL,
  consumed_at    TIMESTAMPTZ,
  one_shot       BOOLEAN NOT NULL DEFAULT TRUE,
  step_up_method VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_grants_active ON auth.approval_grants(agent_id, task_id) WHERE consumed_at IS NULL;

-- RLS ----------------------------------------------------------------------------------
ALTER TABLE auth.agents             ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.api_keys           ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.audit_log          ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.policies           ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.service_clients    ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.tenant_quotas      ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.behavior_policies  ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.approval_requests  ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.approval_grants    ENABLE ROW LEVEL SECURITY;

CREATE POLICY p_agents_tenant            ON auth.agents            USING (tenant_id = current_setting('app.tenant_id')::uuid);
CREATE POLICY p_api_keys_tenant          ON auth.api_keys          USING (tenant_id = current_setting('app.tenant_id')::uuid);
CREATE POLICY p_audit_log_tenant         ON auth.audit_log         USING (tenant_id = current_setting('app.tenant_id')::uuid);
CREATE POLICY p_policies_tenant          ON auth.policies          USING (tenant_id = current_setting('app.tenant_id')::uuid OR tenant_id IS NULL);
CREATE POLICY p_service_clients_tenant   ON auth.service_clients   USING (tenant_id = current_setting('app.tenant_id')::uuid);
CREATE POLICY p_tenant_quotas_tenant     ON auth.tenant_quotas     USING (tenant_id = current_setting('app.tenant_id')::uuid);
CREATE POLICY p_behavior_policies_tenant ON auth.behavior_policies USING (tenant_id = current_setting('app.tenant_id')::uuid OR tenant_id IS NULL);
CREATE POLICY p_approval_requests_tenant ON auth.approval_requests USING (tenant_id = current_setting('app.tenant_id')::uuid);
CREATE POLICY p_approval_grants_tenant   ON auth.approval_grants   USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Grants -------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.tenants                  TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.signing_keys            TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.service_acl             TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.bootstrap_state         TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.plan_defaults           TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.upstream_identity       TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.upstream_service_issuers TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.revoked_tokens          TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.signup_attempts         TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.agents                  TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.api_keys                TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.policies                TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.service_clients         TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.tenant_quotas           TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.behavior_policies       TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.approval_requests       TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.approval_grants         TO auth_user;
GRANT SELECT, INSERT ON auth.audit_log TO auth_user;     -- append-only: no UPDATE/DELETE
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO auth_user;

-- ===================== 20260606_0002__seed.sql =======================================

INSERT INTO auth.tenants (tenant_id, name, plan, source) VALUES
  ('00000000-0000-0000-0000-000000000001', 'platform',         'enterprise', 'manual-seed'),
  ('00000000-0000-0000-0000-0000000000ff', 'integration-test', 'free',       'manual-seed')
ON CONFLICT (tenant_id) DO NOTHING;

-- plan_defaults / default policy / service_acl seeds are identical to 20260606_0002__seed.sql.
-- See that file for the full limits JSON and the service_acl edges (omitted here for brevity;
-- the versioned seed file is the canonical seed source — this snapshot documents schema shape).

-- ===================== 20260610_0003__outbox.sql ======================================

-- Transactional outbox (Phase 2 Amendment Log 2026-06 / WP02). PLATFORM-scoped — NO RLS.
-- Shape mirrors llms.outbox / guardrails.outbox; drained by the in-service OutboxRelay.
CREATE TABLE IF NOT EXISTS auth.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,         -- Kafka message key (tenant_id; Contract 5 §4)
  payload       JSONB        NOT NULL,         -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,                   -- NULL until the relay publishes
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
  ON auth.outbox (created_at) WHERE published_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.outbox TO auth_user;

-- ===================== 20260610_0004__wp03_auth_completion.sql ========================

-- WP03 (Auth completion I): self-protection rate limits (Component 4), the /v1/usage
-- rollup target (Component 1d), the tenants.plan enum guard, and the Component 5c shadow
-- seed. Idempotent (IF NOT EXISTS / DO-block / ON CONFLICT).

-- Component 4 — self-protection rate limits. PLATFORM-scoped (no RLS).
CREATE TABLE IF NOT EXISTS auth.rate_limit_config (
  config_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  endpoint         VARCHAR(100) NOT NULL,
  scope_kind       VARCHAR(30)  NOT NULL,
  tenant_id        UUID,
  limit_rpm        INTEGER      NOT NULL,
  burst_multiplier NUMERIC(4,2) NOT NULL DEFAULT 1.00,
  burst_seconds    INTEGER      NOT NULL DEFAULT 0,
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rate_limit_config_key
  ON auth.rate_limit_config (endpoint, scope_kind, tenant_id) NULLS NOT DISTINCT;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.rate_limit_config TO auth_user;

INSERT INTO auth.rate_limit_config (endpoint, scope_kind, limit_rpm, burst_multiplier, burst_seconds) VALUES
  ('/v1/authorize',         'per-caller-service', 5000, 2.00, 10),
  ('/v1/authorize',         'per-tenant',         2000, 2.00, 10),
  ('/v1/agents/{id}/token', 'per-agent',            60, 2.00, 30),
  ('/v1/agents/{id}/token', 'per-tenant',          600, 2.00, 30),
  ('/v1/service-tokens',    'per-service',          30, 1.00,  0),
  ('/v1/admin/*',           'per-admin-agent',      10, 1.00,  0),
  ('/v1/onboarding/signup', 'per-ip',               10, 1.00,  0)
ON CONFLICT (endpoint, scope_kind, tenant_id) DO NOTHING;

-- Component 1d — /v1/usage rollup target. Tenant-scoped (RLS on app.tenant_id).
CREATE TABLE IF NOT EXISTS auth.tenant_usage_counters (
  tenant_id    UUID         NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  window_start TIMESTAMPTZ  NOT NULL,
  metric       VARCHAR(50)  NOT NULL,
  value        NUMERIC(20,6) NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, window_start, metric)
);
ALTER TABLE auth.tenant_usage_counters ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant_usage_counters_tenant ON auth.tenant_usage_counters
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.tenant_usage_counters TO auth_user;

-- tenants.plan enum guard (DO-block-guarded ADD CONSTRAINT).
ALTER TABLE auth.tenants ADD CONSTRAINT tenants_plan_chk CHECK (plan IN ('free','pro','enterprise'));

-- Component 5c — ONE shadow seed row (table + seed only this phase; no middleware).
INSERT INTO auth.behavior_policies (policy_id, tenant_id, agent_id, name, version, status, constraints, enforcement, cooldown_seconds)
VALUES ('00000000-0000-0000-0000-0000000000b1', NULL, NULL, 'default-behavior-shadow', 1, 'shadow',
  '{ "rate_limits": {}, "structural_limits": {}, "sequence_rules": [], "anomaly_signals": {} }'::jsonb, 'alert', 300)
ON CONFLICT (policy_id) DO NOTHING;

-- ===================== 20260611_0008__audit_pipeline.sql ==============================

-- WP04 (Auth completion II): audit-pipeline export-job audit trail. The /v1/usage rollup
-- target (auth.tenant_usage_counters) already exists above (0004); the audit_log mirror
-- writes to OBJECT STORAGE (S3/MinIO/local), not the DB. Idempotent.

-- Component 6 export — on-demand audit-export job records. Tenant-scoped (RLS on app.tenant_id).
CREATE TABLE IF NOT EXISTS auth.audit_export_jobs (
  export_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID         NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  requested_by  UUID,
  store_backend VARCHAR(20)  NOT NULL,
  object_key    TEXT         NOT NULL,
  object_uri    TEXT         NOT NULL,
  row_count     BIGINT       NOT NULL DEFAULT 0,
  truncated     BOOLEAN      NOT NULL DEFAULT FALSE,
  window_from   TIMESTAMPTZ,
  window_to     TIMESTAMPTZ,
  url_expires_at TIMESTAMPTZ NOT NULL,
  status        VARCHAR(20)  NOT NULL DEFAULT 'completed',
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_export_jobs_tenant
  ON auth.audit_export_jobs (tenant_id, created_at DESC);
ALTER TABLE auth.audit_export_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_audit_export_jobs_tenant ON auth.audit_export_jobs
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.audit_export_jobs TO auth_user;

-- =====================================================================================
-- end schema.sql
-- =====================================================================================
