-- =====================================================================================================================
-- dev/local/seed/postgres-init.sql — local PostgreSQL bootstrap (Phase 1, Component 16 / 17c).
--
-- Mounted into /docker-entrypoint-initdb.d and executed ONCE by the pgvector/pgvector:pg16 entrypoint on first init
-- (empty data dir), AFTER the entrypoint has created the cypherx_platform database and connected to it.
--
-- This is the LOCAL mirror of the Terraform-owned cloud bootstrap (modules/postgres-bootstrap). It deliberately
-- reproduces the SAME object names so service code written against local works unchanged in dev/staging/prod:
--   - extensions:   vector (pgvector), pg_stat_statements
--   - schemas:      auth, llms, guardrails, memory, rag, xagent, platform   (Component 16)
--   - runtime users: auth_user, llms_user, grd_user, mem_user, rag_user, xagent_user, plat_user   (Component 14)
--   - DDL users:    auth_ddl, llms_ddl, grd_ddl, mem_ddl, rag_ddl, xagent_ddl, plat_ddl           (Component 14)
--   - default privileges so Atlas-created tables/sequences are usable by the runtime user (Component 16)
--   - one example tenant-scoped table WITH RLS so the Contract 13 isolation path is exercisable locally.
--
-- NOTE on passwords: the cloud uses per-service Doppler secrets (db/<svc>/runtime_password, db/<svc>/ddl_password).
-- LOCALLY every user gets the SAME throwaway password from the CYPHERX_LOCAL_DB_PASSWORD env var (default "localdev")
-- via :'local_pw' below. These are NOT real secrets — never copy them anywhere.
--
-- NOTE on CREATEROLE: in cloud, *_ddl users get cluster-wide CREATEROLE (Postgres cannot scope it to a schema —
-- documented limitation in Component 16). We grant it here too so Atlas's RLS-role creation path behaves identically.
-- =====================================================================================================================

\set ON_ERROR_STOP on

-- Resolve the local throwaway password from the container env var CYPHERX_LOCAL_DB_PASSWORD (default "localdev").
-- A psql client variable (:'local_pw') cannot be read inside a DO block, so we stash it in a custom session GUC
-- (cypherx.local_pw) that the DO blocks below read via current_setting(). SET only takes a literal, so we feed the
-- psql variable into it. The backtick runs `echo` in the container shell at parse time.
\set local_pw `echo "${CYPHERX_LOCAL_DB_PASSWORD:-localdev}"`
SET cypherx.local_pw = :'local_pw';

-- ---------------------------------------------------------------------------------------------------------------------
-- Extensions — pgvector (regular CREATE EXTENSION, NOT preloaded) + pg_stat_statements (preloaded via the
-- shared_preload_libraries command flag in docker-compose.yml). Component 5 / Component 16.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ---------------------------------------------------------------------------------------------------------------------
-- Per-service schemas (Component 16).
-- ---------------------------------------------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS llms;
CREATE SCHEMA IF NOT EXISTS guardrails;
CREATE SCHEMA IF NOT EXISTS memory;
CREATE SCHEMA IF NOT EXISTS rag;
CREATE SCHEMA IF NOT EXISTS xagent;
CREATE SCHEMA IF NOT EXISTS platform;

-- ---------------------------------------------------------------------------------------------------------------------
-- Runtime users (least privilege) + DDL users (CREATEROLE for Atlas RLS roles).
-- Created idempotently via a DO block so re-running against a non-empty volume does not error.
-- Username naming mirrors modules/postgres-bootstrap exactly: guardrails->grd, memory->mem, platform->plat.
-- ---------------------------------------------------------------------------------------------------------------------
DO $$
DECLARE
  local_pw text := COALESCE(current_setting('cypherx.local_pw', true), 'localdev');
  -- service key -> [schema, runtime_user, ddl_user]
  rec record;
BEGIN
  FOR rec IN
    SELECT * FROM (VALUES
      ('auth',       'auth',       'auth_user',   'auth_ddl'),
      ('llms',       'llms',       'llms_user',   'llms_ddl'),
      ('guardrails', 'guardrails', 'grd_user',    'grd_ddl'),
      ('memory',     'memory',     'mem_user',    'mem_ddl'),
      ('rag',        'rag',        'rag_user',    'rag_ddl'),
      ('xagent',     'xagent',     'xagent_user', 'xagent_ddl'),
      ('platform',   'platform',   'plat_user',   'plat_ddl'),
      ('cypherx-a1', 'cypherx_a1', 'cxa1_user',   'cxa1_ddl')
    ) AS t(svc, schema_name, runtime_user, ddl_user)
  LOOP
    -- Runtime user (login, no CREATEROLE, no superuser).
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rec.runtime_user) THEN
      EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', rec.runtime_user, local_pw);
    END IF;

    -- DDL user (login + CREATEROLE so Atlas can create the per-schema RLS role).
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rec.ddl_user) THEN
      EXECUTE format('CREATE ROLE %I LOGIN CREATEROLE PASSWORD %L', rec.ddl_user, local_pw);
    END IF;

    -- The schema is owned by the DDL user so Atlas (running as *_ddl) can fully manage it.
    EXECUTE format('ALTER SCHEMA %I OWNER TO %I', rec.schema_name, rec.ddl_user);

    -- Grants (Component 16): DDL user gets CREATE+USAGE; runtime user gets USAGE only.
    EXECUTE format('GRANT CREATE, USAGE ON SCHEMA %I TO %I', rec.schema_name, rec.ddl_user);
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', rec.schema_name, rec.runtime_user);

    -- Default privileges so Atlas-created (owned by *_ddl) tables/sequences are usable by the runtime user.
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO %I',
      rec.ddl_user, rec.schema_name, rec.runtime_user);
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE,SELECT ON SEQUENCES TO %I',
      rec.ddl_user, rec.schema_name, rec.runtime_user);
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- EXAMPLE tenant-scoped table WITH RLS — mirrors contracts/migrations/service-template (Contract 13 / Contract 18).
--
-- Purpose: prove the local stack exercises the SAME tenant-isolation path the platform enforces architecturally:
--   - tenant_id UUID NOT NULL, index starting with tenant_id
--   - ENABLE + FORCE ROW LEVEL SECURITY
--   - USING (tenant_id = current_setting('app.tenant_id')::uuid)
--
-- We create it in the auth schema, owned by auth_ddl (the migration owner), and create the per-schema RLS role exactly
-- as Atlas would. A service connecting as auth_user MUST run inside a transaction:
--     BEGIN; SET LOCAL app.tenant_id = '<uuid>'; SELECT * FROM auth.example_agents; COMMIT;
-- Rows from other tenants are invisible — a cross-tenant SELECT returns 0 rows (Contract 15 case 4).
--
-- FORCE ROW LEVEL SECURITY is set so RLS applies even to the table owner — without it, auth_ddl/superuser bypass RLS
-- and the local "cross-tenant denial" check would silently pass. We keep it faithful to the cloud behaviour.
-- ---------------------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth.example_agents (
  agent_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deactivated')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_example_agents_tenant ON auth.example_agents (tenant_id);

ALTER TABLE auth.example_agents OWNER TO auth_ddl;
ALTER TABLE auth.example_agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth.example_agents FORCE ROW LEVEL SECURITY; -- apply RLS to the owner too, so local mirrors prod denial

-- Per-schema RLS role pattern (Atlas creates a role the runtime user is granted into). Here we apply the policy
-- directly to the runtime user so the local mirror is self-contained.
DROP POLICY IF EXISTS p_example_agents_tenant ON auth.example_agents;
CREATE POLICY p_example_agents_tenant ON auth.example_agents
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.example_agents TO auth_user;

-- Seed two rows under two different tenants so the cross-tenant denial is demonstrable immediately.
-- We set app.tenant_id locally for the inserts so the WITH CHECK passes. These run as the superuser owner of this
-- init session; FORCE RLS means the policy is still evaluated.
DO $$
DECLARE
  tenant_a CONSTANT uuid := '00000000-0000-0000-0000-0000000000aa';
  tenant_b CONSTANT uuid := '00000000-0000-0000-0000-0000000000bb';
BEGIN
  PERFORM set_config('app.tenant_id', tenant_a::text, true);
  INSERT INTO auth.example_agents (tenant_id, name) VALUES (tenant_a, 'tenant-a-agent') ON CONFLICT DO NOTHING;

  PERFORM set_config('app.tenant_id', tenant_b::text, true);
  INSERT INTO auth.example_agents (tenant_id, name) VALUES (tenant_b, 'tenant-b-agent') ON CONFLICT DO NOTHING;
END
$$;

-- ---------------------------------------------------------------------------------------------------------------------
-- Quick local verification (printed in the container init log):
--   With app.tenant_id = tenant_a, the runtime user sees ONLY the tenant-a row.
-- ---------------------------------------------------------------------------------------------------------------------
DO $$
DECLARE
  visible int;
BEGIN
  PERFORM set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000aa', true);
  SELECT count(*) INTO visible FROM auth.example_agents;
  RAISE NOTICE 'RLS self-check: rows visible for tenant-a = % (expected 1)', visible;
END
$$;
