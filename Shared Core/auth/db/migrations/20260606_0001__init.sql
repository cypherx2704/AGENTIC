-- =====================================================================================
-- auth-service — first-cycle schema (Phase 2). PostgreSQL 16.
--
-- Run as a superuser / migration role. Creates the `auth` schema, the runtime role
-- `auth_user`, every first-cycle table, indexes, Row Level Security (Contract 13), and
-- the grants the runtime role needs.
--
-- TENANT-SCOPED tables (tenant_id + tenant-leading index + RLS USING app.tenant_id):
--   agents, api_keys, audit_log, policies(*), service_clients, tenant_quotas,
--   behavior_policies, approval_requests, approval_grants
--   (*) policies holds BOTH platform-default rows (tenant_id IS NULL) and per-tenant rows;
--       its RLS policy admits NULL-tenant rows so every tenant can read platform defaults.
--
-- PLATFORM-SCOPED tables (NO tenant_id semantics, NO RLS — mutated by Auth itself only):
--   tenants, signing_keys, service_acl, bootstrap_state, plan_defaults,
--   upstream_identity, upstream_service_issuers, revoked_tokens, signup_attempts
--
-- The runtime role connects with `currentSchema=auth` and runs every tenant-scoped query
-- inside BEGIN; SET LOCAL app.tenant_id=...; ...; COMMIT (Core TenantTx helper).
-- The runtime role is NOT a superuser and does NOT BYPASSRLS, so RLS is enforced.
-- =====================================================================================

-- ── Extensions (gen_random_uuid, CITEXT for emails) ──────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ── Schema ────────────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS auth;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
-- NOTE: NOT a superuser, no BYPASSRLS — RLS must apply. Password set out-of-band / by infra.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'auth_user') THEN
    CREATE ROLE auth_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA auth TO auth_user;

-- =====================================================================================
-- PLATFORM-SCOPED TABLES (no RLS)
-- =====================================================================================

-- ── tenants ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth.tenants (
  tenant_id            UUID PRIMARY KEY,
  name                 VARCHAR(255) NOT NULL,
  status               VARCHAR(20)  NOT NULL DEFAULT 'active',
                       -- active | pending_verification | suspended | pending_deletion | deleted
  plan                 VARCHAR(50)  NOT NULL DEFAULT 'free',
  source               VARCHAR(30)  NOT NULL DEFAULT 'manual-seed',
                       -- px0-bridge | external-admin | self-serve-signup | sso-jit | manual-seed
  source_metadata      JSONB        NOT NULL DEFAULT '{}',
  region               VARCHAR(20)  NOT NULL DEFAULT 'us-east-1',
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  suspended_at         TIMESTAMPTZ,
  pending_deletion_at  TIMESTAMPTZ,
  deleted_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON auth.tenants(status);

-- ── signing_keys (RS256 key material; envelope-encrypted private PEM) ─────────────────
CREATE TABLE IF NOT EXISTS auth.signing_keys (
  kid              UUID PRIMARY KEY,
  private_pem_enc  BYTEA       NOT NULL,   -- KeyEncryptor (local AES / KMS) envelope of the private PEM
  public_jwk       JSONB       NOT NULL,   -- public key in JWK form (clear)
  status           VARCHAR(20) NOT NULL,   -- signing | verifying | retired
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  promoted_at      TIMESTAMPTZ,
  retired_at       TIMESTAMPTZ,
  CONSTRAINT signing_keys_status_chk CHECK (status IN ('signing','verifying','retired'))
);
-- Exactly ONE signing key at any moment (partial unique index makes rotation swaps atomic):
CREATE UNIQUE INDEX IF NOT EXISTS one_signing_key
  ON auth.signing_keys (status)
  WHERE status = 'signing';
CREATE INDEX IF NOT EXISTS idx_signing_keys_status ON auth.signing_keys(status);

-- ── service_acl (which service may call which, and with what scopes) ──────────────────
CREATE TABLE IF NOT EXISTS auth.service_acl (
  caller_service  VARCHAR(100) NOT NULL,
  target_service  VARCHAR(100) NOT NULL,
  allowed_scopes  TEXT[]       NOT NULL,
  PRIMARY KEY (caller_service, target_service)
);

-- ── bootstrap_state (sentinel marking one-time super-admin bootstrap complete) ────────
CREATE TABLE IF NOT EXISTS auth.bootstrap_state (
  id            BOOLEAN PRIMARY KEY DEFAULT TRUE,   -- single-row table (id is always TRUE)
  completed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_by  UUID,
  CONSTRAINT bootstrap_state_singleton CHECK (id = TRUE)
);

-- ── plan_defaults (Contract 19 — default quota limits per plan) ───────────────────────
CREATE TABLE IF NOT EXISTS auth.plan_defaults (
  plan    VARCHAR(50) PRIMARY KEY,
  limits  JSONB NOT NULL
);

-- ── upstream_identity (Component 11 — px0 / federated IdP trust anchors) ───────────────
CREATE TABLE IF NOT EXISTS auth.upstream_identity (
  issuer        VARCHAR(500) PRIMARY KEY,           -- "px0" / issuer URL
  jwks_url      VARCHAR(500) NOT NULL,
  audience      VARCHAR(255) NOT NULL,
  root_jwk_pem  BYTEA        NOT NULL,              -- pinned root for signed-bundle verify
  status        VARCHAR(20)  NOT NULL DEFAULT 'active',
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── upstream_service_issuers (Component 8b-ext — federated OIDC service issuers) ──────
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

-- ── revoked_tokens (Component 3c — durable jti revocation record) ─────────────────────
CREATE TABLE IF NOT EXISTS auth.revoked_tokens (
  jti          UUID PRIMARY KEY,
  agent_id     UUID,
  tenant_id    UUID        NOT NULL,
  revoked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_by   UUID        NOT NULL,
  reason       VARCHAR(50) NOT NULL,
                -- compromised | rotated | deactivated | policy_violation | admin_action
  token_exp    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revoked_purge ON auth.revoked_tokens(token_exp);

-- ── signup_attempts (Component 1c — self-serve onboarding) ────────────────────────────
CREATE TABLE IF NOT EXISTS auth.signup_attempts (
  signup_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email                   CITEXT      NOT NULL,
  full_name               TEXT        NOT NULL,
  intended_use            TEXT,
  terms_version_accepted  TEXT        NOT NULL,
  verification_token      TEXT        NOT NULL UNIQUE,
  verification_expires_at TIMESTAMPTZ NOT NULL,
  verified_at             TIMESTAMPTZ,
  tenant_id               UUID,
  initial_admin_user_id   UUID,
  risk_score              NUMERIC(3,2) NOT NULL DEFAULT 0.00,
  risk_signals            JSONB        NOT NULL DEFAULT '{}',
  status                  VARCHAR(30)  NOT NULL DEFAULT 'pending_verification',
                          -- pending_verification | manual_review | verified | rejected
  ip_address              INET,
  user_agent              TEXT,
  created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_signup_email      ON auth.signup_attempts (email);
CREATE INDEX IF NOT EXISTS ix_signup_ip_created ON auth.signup_attempts (ip_address, created_at);

-- =====================================================================================
-- TENANT-SCOPED TABLES (tenant_id + tenant-leading index + RLS)
-- =====================================================================================

-- ── agents ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth.agents (
  agent_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL,
  name             VARCHAR(255) NOT NULL,
  description      TEXT,
  version          VARCHAR(50)  NOT NULL DEFAULT '1.0.0',
  status           VARCHAR(20)  NOT NULL DEFAULT 'active',
                   -- active | inactive | suspended | quarantined
  capabilities     JSONB        NOT NULL DEFAULT '[]',
  allowed_scopes   TEXT[]       NOT NULL DEFAULT '{}',
  allowed_tools    TEXT[]       NOT NULL DEFAULT '{}',
  allowed_skills   TEXT[]       NOT NULL DEFAULT '{}',
  metadata         JSONB        NOT NULL DEFAULT '{}',
  quarantine_until TIMESTAMPTZ,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  created_by       UUID         NOT NULL,   -- px0 user_id; SYSTEM-USER sentinel for bootstrap/seed
  CONSTRAINT agents_tenant_name_version_unique UNIQUE (tenant_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_agents_tenant_id ON auth.agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agents_status    ON auth.agents(tenant_id, status);

-- ── api_keys (auth-service's own keys; key_hash = SHA-256 of raw key per Component 2) ─
CREATE TABLE IF NOT EXISTS auth.api_keys (
  key_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id      UUID NOT NULL REFERENCES auth.agents(agent_id),
  tenant_id     UUID NOT NULL,
  key_hash      VARCHAR(64) NOT NULL UNIQUE,   -- SHA-256 hex of the raw key — raw never stored
  key_prefix    VARCHAR(20) NOT NULL,          -- first chars for display
  name          VARCHAR(255),
  scopes        TEXT[]      NOT NULL DEFAULT '{}',
  status        VARCHAR(20) NOT NULL DEFAULT 'active',  -- active | revoked | expired | rotating
  expires_at    TIMESTAMPTZ,
  last_used_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at    TIMESTAMPTZ,
  revoked_by    UUID
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_agent ON auth.api_keys(tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash     ON auth.api_keys(key_hash);

-- ── policies (Component 5 — RBAC; platform-default rows have tenant_id NULL) ──────────
CREATE TABLE IF NOT EXISTS auth.policies (
  policy_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID,                          -- NULL = platform default policy
  name         VARCHAR(255) NOT NULL,
  description  TEXT,
  version      INTEGER      NOT NULL DEFAULT 1,
  status       VARCHAR(20)  NOT NULL DEFAULT 'active',
  rules        JSONB        NOT NULL,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_policies_tenant ON auth.policies(tenant_id);

-- ── audit_log (Component 6 — append-only hash chain) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS auth.audit_log (
  id            BIGSERIAL PRIMARY KEY,
  event_type    VARCHAR(50) NOT NULL,
  agent_id      UUID,
  tenant_id     UUID        NOT NULL,
  action        VARCHAR(100),
  resource      VARCHAR(255),
  decision      VARCHAR(10),                  -- allow | deny
  policy_ids    TEXT[],
  request_id    UUID,
  trace_id      UUID,
  ip_address    INET,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Tamper-evidence (per-tenant hash chain; genesis row uses 32 zero bytes):
  row_hash      BYTEA NOT NULL,
  prev_row_hash BYTEA NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_id  ON auth.audit_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent_id   ON auth.audit_log(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON auth.audit_log(event_type, created_at DESC);

-- ── service_clients (Component 8b-ext — external OAuth2 client_credentials clients) ───
CREATE TABLE IF NOT EXISTS auth.service_clients (
  client_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  name                 TEXT NOT NULL,
  client_secret_hash   TEXT,                  -- Argon2id; NULL if federated-only
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

-- ── tenant_quotas (Component 1d — per-tenant effective quotas) ────────────────────────
CREATE TABLE IF NOT EXISTS auth.tenant_quotas (
  tenant_id        UUID        NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  plan             VARCHAR(50) NOT NULL REFERENCES auth.plan_defaults(plan),
  limits           JSONB       NOT NULL,
  effective_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  effective_until  TIMESTAMPTZ,
  source           VARCHAR(30) NOT NULL,      -- plan-default | admin-override | billing-event
  updated_by       TEXT        NOT NULL,
  PRIMARY KEY (tenant_id, effective_from)
);
CREATE INDEX IF NOT EXISTS ix_tenant_quotas_current
  ON auth.tenant_quotas (tenant_id) WHERE effective_until IS NULL;

-- ── behavior_policies (Component 5c — runtime behavioral envelopes) ───────────────────
CREATE TABLE IF NOT EXISTS auth.behavior_policies (
  policy_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID,                      -- NULL = platform default
  agent_id         UUID,                      -- NULL = all agents in tenant
  name             VARCHAR(255) NOT NULL,
  version          INTEGER      NOT NULL DEFAULT 1,
  status           VARCHAR(20)  NOT NULL DEFAULT 'active',   -- active | shadow | suspended
  constraints      JSONB        NOT NULL,
  enforcement      VARCHAR(20)  NOT NULL DEFAULT 'block',    -- block | quarantine | alert
  cooldown_seconds INTEGER      NOT NULL DEFAULT 300,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_behavior_tenant
  ON auth.behavior_policies(tenant_id, agent_id) WHERE status = 'active';

-- ── approval_requests (Component 10 — step-up approval requests) ──────────────────────
CREATE TABLE IF NOT EXISTS auth.approval_requests (
  request_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID   NOT NULL,
  agent_id        UUID   NOT NULL,
  task_id         UUID,
  scopes          TEXT[] NOT NULL,
  resource        VARCHAR(500),
  reason          TEXT,
  context         JSONB,
  status          VARCHAR(20) NOT NULL DEFAULT 'pending',   -- pending | granted | denied | expired
  requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ NOT NULL,
  resolved_at     TIMESTAMPTZ,
  resolved_by     UUID,
  resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_pending
  ON auth.approval_requests(tenant_id, status, expires_at) WHERE status = 'pending';

-- ── approval_grants (Component 10 — granted, signed, short-lived approvals) ────────────
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
CREATE INDEX IF NOT EXISTS idx_grants_active
  ON auth.approval_grants(agent_id, task_id) WHERE consumed_at IS NULL;

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — tenant-scoped tables only
-- Every tenant-scoped query runs inside a tx that does SET LOCAL app.tenant_id = '<uuid>'.
-- The policy admits rows where tenant_id matches app.tenant_id. For tables that also hold
-- platform-default rows (policies, behavior_policies) the policy ALSO admits tenant_id IS NULL
-- so a tenant transaction can read the platform default.
-- =====================================================================================

ALTER TABLE auth.agents             ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.api_keys           ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.audit_log          ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.policies           ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.service_clients    ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.tenant_quotas      ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.behavior_policies  ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.approval_requests  ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.approval_grants    ENABLE ROW LEVEL SECURITY;

-- Idempotent (re-runnable migrate): DROP each policy before re-creating it. Bare CREATE POLICY
-- is NOT idempotent and crashed the one-shot migrate job on any re-run ("policy already exists").
DROP POLICY IF EXISTS p_agents_tenant ON auth.agents;
CREATE POLICY p_agents_tenant ON auth.agents
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

DROP POLICY IF EXISTS p_api_keys_tenant ON auth.api_keys;
CREATE POLICY p_api_keys_tenant ON auth.api_keys
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

DROP POLICY IF EXISTS p_audit_log_tenant ON auth.audit_log;
CREATE POLICY p_audit_log_tenant ON auth.audit_log
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- policies: per-tenant rows OR platform-default rows (tenant_id IS NULL) are visible.
DROP POLICY IF EXISTS p_policies_tenant ON auth.policies;
CREATE POLICY p_policies_tenant ON auth.policies
  USING (tenant_id = current_setting('app.tenant_id')::uuid OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_service_clients_tenant ON auth.service_clients;
CREATE POLICY p_service_clients_tenant ON auth.service_clients
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

DROP POLICY IF EXISTS p_tenant_quotas_tenant ON auth.tenant_quotas;
CREATE POLICY p_tenant_quotas_tenant ON auth.tenant_quotas
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

DROP POLICY IF EXISTS p_behavior_policies_tenant ON auth.behavior_policies;
CREATE POLICY p_behavior_policies_tenant ON auth.behavior_policies
  USING (tenant_id = current_setting('app.tenant_id')::uuid OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_approval_requests_tenant ON auth.approval_requests;
CREATE POLICY p_approval_requests_tenant ON auth.approval_requests
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

DROP POLICY IF EXISTS p_approval_grants_tenant ON auth.approval_grants;
CREATE POLICY p_approval_grants_tenant ON auth.approval_grants
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- =====================================================================================
-- GRANTS to the runtime role (auth_user)
-- =====================================================================================

-- Platform-scoped tables: the runtime role reads/writes signing keys, tenants, etc.
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.tenants                  TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.signing_keys            TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.service_acl             TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.bootstrap_state         TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.plan_defaults           TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.upstream_identity       TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.upstream_service_issuers TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.revoked_tokens          TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.signup_attempts         TO auth_user;

-- Tenant-scoped tables (RLS still applies on top of these grants):
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.agents                  TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.api_keys                TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.policies                TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.service_clients         TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.tenant_quotas           TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.behavior_policies       TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.approval_requests       TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.approval_grants         TO auth_user;

-- audit_log is APPEND-ONLY for the runtime role: SELECT + INSERT only.
-- UPDATE/DELETE are deliberately NOT granted (tamper-evidence defence; Component 6).
-- A separate retention-purge role (out of first-cycle scope) holds DELETE.
GRANT SELECT, INSERT ON auth.audit_log TO auth_user;

-- Sequences the runtime role needs (audit_log BIGSERIAL):
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO auth_user;

-- =====================================================================================
-- end 20260606_0001__init.sql
-- =====================================================================================
