-- =====================================================================================
-- tool-flow-bridge — first-cycle schema. PostgreSQL 16. Idempotent.
--
-- Run as a superuser / migration role. Creates the `flow_tools` schema, its two tables,
-- indexes, Row Level Security (Contract 13), and the grants the runtime role
-- `flow_tools_user` needs.
--
-- TABLES (tenant-owned; no platform rows — every flow-tool belongs to a tenant):
--   tenant_runtimes  — one Node-RED instance per tenant (the execution backend)
--   tool_bindings    — one published workflow -> MCP tool binding
--
-- SPLIT RLS (marketplace-hole fix, mirrors tool-registry): a permissive SELECT policy
-- (own rows) + a separate write policy whose WITH CHECK rejects writing another tenant's
-- id. A dedicated EMPTY-GUC "platform" policy serves the UNAUTHENTICATED manifest endpoint
-- (GET /w/<slug>/manifest, polled by the Tool Registry) and any platform reconciliation:
-- it may read bindings by (globally-unique) slug when app.tenant_id is empty. Manifests are
-- non-secret self-descriptions; no secret material is ever stored in these tables (only refs).
--
-- The runtime role connects and runs every tenant-scoped query inside
--   BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT   (in_tenant()).
-- The manifest endpoint runs with an EMPTY app.tenant_id (in_platform()).
-- It is NOT a superuser and does NOT BYPASSRLS.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS flow_tools;

SET search_path = flow_tools, public;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flow_tools_user') THEN
    CREATE ROLE flow_tools_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA flow_tools TO flow_tools_user;

-- =====================================================================================
-- TABLES
-- =====================================================================================

-- ── tenant_runtimes (one Node-RED instance per tenant) ────────────────────────────────
CREATE TABLE IF NOT EXISTS flow_tools.tenant_runtimes (
  runtime_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL,
  status                VARCHAR(20) NOT NULL DEFAULT 'provisioning', -- provisioning|running|stopped|error
  internal_host         TEXT NOT NULL,               -- e.g. http://nodered-<tenant>.tools.svc:1880
  http_node_root        VARCHAR(80) NOT NULL DEFAULT '/flow',
  admin_token_ref       TEXT NOT NULL,               -- secret ref for the Node-RED Admin API bearer
  invoke_secret_ref     TEXT NOT NULL,               -- secret ref for the HTTP-In header auth
  credential_secret_ref TEXT NOT NULL,               -- secret ref for Node-RED credentialSecret
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_runtimes_tenant ON flow_tools.tenant_runtimes (tenant_id);

-- ── tool_bindings (published workflow -> MCP tool) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS flow_tools.tool_bindings (
  binding_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL,
  slug             VARCHAR(120) NOT NULL,     -- globally-unique dash-case; registry server = tool-<slug>
  snake_name       VARCHAR(100) NOT NULL,     -- MCP tool name (snake_case)
  display_name     VARCHAR(200) NOT NULL,
  description      TEXT NOT NULL,
  runtime_id       UUID NOT NULL REFERENCES flow_tools.tenant_runtimes (runtime_id) ON DELETE RESTRICT,
  node_red_flow_id TEXT NOT NULL,
  http_method      VARCHAR(10) NOT NULL DEFAULT 'POST',
  http_path        TEXT NOT NULL,             -- the HTTP-In node path (under http_node_root)
  input_schema     JSONB NOT NULL,
  output_schema    JSONB,
  manifest         JSONB NOT NULL,            -- exact Contract-4 manifest last registered
  version          VARCHAR(40) NOT NULL DEFAULT '1.0.0',
  access_mode      VARCHAR(15) NOT NULL DEFAULT 'ask',   -- none|ask|automated
  status           VARCHAR(20) NOT NULL DEFAULT 'active', -- active|retired
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT tool_bindings_access_chk CHECK (access_mode IN ('none','ask','automated'))
);
-- slug is globally unique so the unauthenticated manifest endpoint can resolve it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_bindings_slug ON flow_tools.tool_bindings (slug);
CREATE INDEX IF NOT EXISTS idx_tool_bindings_tenant ON flow_tools.tool_bindings (tenant_id, status);

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13)
-- =====================================================================================

ALTER TABLE flow_tools.tenant_runtimes ENABLE ROW LEVEL SECURITY;
ALTER TABLE flow_tools.tool_bindings   ENABLE ROW LEVEL SECURITY;

-- current tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid
-- platform ctx   := NULLIF(current_setting('app.tenant_id', true), '') IS NULL  (empty GUC)

-- ── tenant_runtimes ───────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS p_tenant_runtimes_read ON flow_tools.tenant_runtimes;
CREATE POLICY p_tenant_runtimes_read ON flow_tools.tenant_runtimes FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_tenant_runtimes_write ON flow_tools.tenant_runtimes;
CREATE POLICY p_tenant_runtimes_write ON flow_tools.tenant_runtimes FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Platform context (empty GUC): a reconciler/provisioner may manage any runtime. Also
-- lets the manifest endpoint (empty GUC) join runtimes if ever needed. Never applies to a
-- tenant request (which always has a non-empty GUC).
DROP POLICY IF EXISTS p_tenant_runtimes_platform ON flow_tools.tenant_runtimes;
CREATE POLICY p_tenant_runtimes_platform ON flow_tools.tenant_runtimes FOR ALL
  USING      (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── tool_bindings ─────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS p_tool_bindings_read ON flow_tools.tool_bindings;
CREATE POLICY p_tool_bindings_read ON flow_tools.tool_bindings FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_tool_bindings_write ON flow_tools.tool_bindings;
CREATE POLICY p_tool_bindings_write ON flow_tools.tool_bindings FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Platform-context READ ONLY: the UNAUTHENTICATED GET /w/<slug>/manifest endpoint (polled
-- by the Tool Registry) resolves a binding by its globally-unique slug when app.tenant_id
-- is empty. Manifests are non-secret; the tables hold no secret material (only refs).
DROP POLICY IF EXISTS p_tool_bindings_platform_read ON flow_tools.tool_bindings;
CREATE POLICY p_tool_bindings_platform_read ON flow_tools.tool_bindings FOR SELECT
  USING (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- =====================================================================================
-- GRANTS to the runtime role (flow_tools_user). RLS still applies on top of these.
-- =====================================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON flow_tools.tenant_runtimes TO flow_tools_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON flow_tools.tool_bindings   TO flow_tools_user;
