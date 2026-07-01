-- =====================================================================================
-- auth-service — user identity (email/password + Google OAuth) + orchestrator agent model.
-- PostgreSQL 16. Idempotent (re-runnable). Apply AFTER 20260614_0009.
--
-- Adds:
--   * auth.users           — end-user login identities (PLATFORM-SCOPED, like signup_attempts:
--                            login resolves a user by email BEFORE any tenant context exists, so
--                            this table has NO RLS and is read/written via TenantTx.inPlatform()).
--   * auth.agents          — orchestrator hierarchy columns (agent_type, parent_orchestrator_id,
--                            immutable_llm, owner_user_id) + a partial unique index enforcing
--                            exactly ONE orchestrator per tenant.
--
-- Design note (login-by-email): auth.users.email is GLOBALLY unique. Each self-serve signup
-- creates one tenant + one user + one orchestrator agent, so an email maps to a single account
-- and login(email,password) can resolve the user without a tenant hint. CITEXT makes the
-- uniqueness case-insensitive.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- =====================================================================================
-- PLATFORM-SCOPED: auth.users (no RLS — mirrors signup_attempts/tenants)
-- =====================================================================================
CREATE TABLE IF NOT EXISTS auth.users (
  user_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  email           CITEXT NOT NULL,
  password_hash   TEXT,                                   -- Argon2id; NULL when login_provider='google'
  login_provider  VARCHAR(20)  NOT NULL DEFAULT 'local',  -- local | google
  google_sub      TEXT,                                   -- Google OIDC subject; NULL for local users
  display_name    TEXT,
  status          VARCHAR(20)  NOT NULL DEFAULT 'active',  -- active | suspended | deleted
  email_verified  BOOLEAN      NOT NULL DEFAULT false,
  last_login_at   TIMESTAMPTZ,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT users_login_provider_chk CHECK (login_provider IN ('local','google')),
  CONSTRAINT users_status_chk         CHECK (status IN ('active','suspended','deleted'))
);

-- Global email uniqueness (login-by-email). Case-insensitive via CITEXT.
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email ON auth.users (email);
-- Google subject uniqueness (only for rows that have one).
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google_sub
  ON auth.users (google_sub) WHERE google_sub IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_tenant ON auth.users (tenant_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.users TO auth_user;

-- =====================================================================================
-- TENANT-SCOPED: auth.agents — orchestrator hierarchy columns
-- (RLS policy p_agents_tenant already enforces tenant isolation; columns are additive.)
-- =====================================================================================
ALTER TABLE auth.agents
  ADD COLUMN IF NOT EXISTS agent_type VARCHAR(20) NOT NULL DEFAULT 'user_created',
  ADD COLUMN IF NOT EXISTS parent_orchestrator_id UUID REFERENCES auth.agents(agent_id),
  ADD COLUMN IF NOT EXISTS immutable_llm BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS owner_user_id UUID;

-- CHECK constraint added separately so the migration is idempotent (ADD COLUMN IF NOT EXISTS
-- cannot carry a named constraint re-runnably).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'agents_agent_type_chk'
  ) THEN
    ALTER TABLE auth.agents
      ADD CONSTRAINT agents_agent_type_chk
      CHECK (agent_type IN ('orchestrator','sub_agent','user_created'));
  END IF;
END
$$;

-- Exactly ONE orchestrator per tenant. Enforced physically (storage-level unique index applies
-- across all rows regardless of RLS visibility), so a tenant can never end up with two.
CREATE UNIQUE INDEX IF NOT EXISTS uq_orchestrator_per_tenant
  ON auth.agents (tenant_id) WHERE agent_type = 'orchestrator';

-- Hierarchy lookups: list sub-agents of an orchestrator.
CREATE INDEX IF NOT EXISTS idx_agents_parent_orchestrator
  ON auth.agents (parent_orchestrator_id) WHERE parent_orchestrator_id IS NOT NULL;

-- =====================================================================================
-- end 20260623_0010__user_auth_and_orchestrator.sql
-- =====================================================================================
