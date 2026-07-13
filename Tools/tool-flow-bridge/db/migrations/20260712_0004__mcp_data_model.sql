-- =====================================================================================
-- tool-flow-bridge — atomic-tool + MCP-collection data model. PostgreSQL 16. Idempotent.
--
-- Replaces the rigid "1 flow = 1 slug = 1 single-tool server" shape (flow_tools.tool_bindings)
-- with:
--   flow_tools.tools      — the ATOMIC tool: one capability built from ONE Node-RED flow.
--   flow_tools.mcps       — an MCP: a named COLLECTION registered to the registry as one server
--                           whose manifest tools[] lists all member tools (an aggregating server).
--   flow_tools.mcp_tools  — (mcp_id, tool_id) many-to-many membership; a tool may join >1 MCP.
--
-- SPLIT RLS (mirrors 20260711_0001__init.sql exactly): a permissive SELECT policy (own rows) +
-- a separate write policy whose WITH CHECK rejects writing another tenant's id + a dedicated
-- EMPTY-GUC "platform" SELECT policy so the UNAUTHENTICATED aggregating manifest endpoint
-- (GET /m/<slug>/manifest, polled by the Tool Registry) can resolve an MCP + its members by the
-- MCP's globally-unique slug when app.tenant_id is empty. Manifests are non-secret; these tables
-- hold no secret material (only the tools' runtime_id -> tenant_runtimes, whose secret refs stay
-- unreadable in platform context because tenant_runtimes has NO platform policy — see 0003).
-- `protected` cross-tenant grants are a FUTURE extension point: for now protected behaves like
-- private (owner-only) PLUS platform stays readable; add the grant-join then.
--
-- DATA MIGRATION (idempotent, one-shot backfill): for every existing flow_tools.tool_bindings
-- row create one flow_tools.tools row (copy fields), one SINGLETON flow_tools.mcps row whose
-- server_name = 'tool-<slug>' (== the binding's existing registry key, so already-registered
-- tools KEEP resolving) and whose slug = the binding's slug (so /m/<slug> and the legacy
-- /w/<slug> resolve the same collection), and one mcp_tools link. tool_id/mcp_id reuse the
-- source binding_id so the backfill is stable + re-runnable.
--
-- flow_tools.tool_bindings is KEPT IN PLACE (NOT dropped): it remains the source of truth for the
-- legacy /w/<slug> invoke + manifest wire and the current publish/list/get/unpublish/test paths.
-- ** flow_tools.tool_bindings is SUPERSEDED by flow_tools.tools + flow_tools.mcps + mcp_tools. **
-- The publish path is rewired to write the new tables in Phase 2 (flow-bridge publish/MCP mgmt
-- API); until then this backfill is the bridge that surfaces existing tools under the new model.
-- =====================================================================================

SET search_path = flow_tools, public;

-- =====================================================================================
-- TABLES
-- =====================================================================================

-- ── tools (the atomic tool: one capability = one Node-RED flow) ───────────────────────
CREATE TABLE IF NOT EXISTS flow_tools.tools (
  tool_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL,                        -- tenant-owned for now (no platform tools yet)
  snake_name       VARCHAR(100) NOT NULL,                -- MCP tool name (snake_case, Contract-4)
  display_name     VARCHAR(200) NOT NULL,
  description      TEXT NOT NULL,
  input_schema     JSONB NOT NULL,
  output_schema    JSONB,
  node_red_flow_id TEXT NOT NULL,
  http_method      VARCHAR(10) NOT NULL DEFAULT 'POST',
  http_path        TEXT NOT NULL,                        -- the HTTP-In node path (under http_node_root)
  runtime_id       UUID NOT NULL REFERENCES flow_tools.tenant_runtimes (runtime_id) ON DELETE RESTRICT,
  visibility       VARCHAR(15) NOT NULL DEFAULT 'private',  -- private|protected|public
  access_mode      VARCHAR(15) NOT NULL DEFAULT 'ask',      -- none|ask|automated (per-tool default)
  status           VARCHAR(20) NOT NULL DEFAULT 'active',   -- active|retired
  version          VARCHAR(40) NOT NULL DEFAULT '1.0.0',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT tools_visibility_chk  CHECK (visibility  IN ('private','protected','public')),
  CONSTRAINT tools_access_mode_chk CHECK (access_mode IN ('none','ask','automated'))
);
CREATE INDEX IF NOT EXISTS idx_tools_tenant     ON flow_tools.tools (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_tools_snake_name ON flow_tools.tools (tenant_id, snake_name);

-- ── mcps (the collection / aggregating server) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS flow_tools.mcps (
  mcp_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID NOT NULL,
  slug         VARCHAR(120) NOT NULL,   -- globally-unique dash-case; the aggregating base is /m/<slug>
  server_name  VARCHAR(140) NOT NULL,   -- registry key: = slug for new MCPs, = tool-<slug> for migrated singletons
  display_name VARCHAR(200) NOT NULL,
  description  TEXT NOT NULL,
  visibility   VARCHAR(15) NOT NULL DEFAULT 'private',   -- private|protected|public
  status       VARCHAR(20) NOT NULL DEFAULT 'active',    -- active|retired
  version      VARCHAR(40) NOT NULL DEFAULT '1.0.0',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT mcps_visibility_chk CHECK (visibility IN ('private','protected','public'))
);
-- slug + server_name are globally unique so the unauthenticated manifest endpoint + the registry
-- can resolve an MCP by either key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mcps_slug        ON flow_tools.mcps (slug);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mcps_server_name ON flow_tools.mcps (server_name);
CREATE INDEX IF NOT EXISTS idx_mcps_tenant ON flow_tools.mcps (tenant_id, status);

-- ── mcp_tools (many-to-many membership; a tool may belong to several MCPs) ─────────────
CREATE TABLE IF NOT EXISTS flow_tools.mcp_tools (
  mcp_id     UUID NOT NULL REFERENCES flow_tools.mcps  (mcp_id)  ON DELETE CASCADE,
  tool_id    UUID NOT NULL REFERENCES flow_tools.tools (tool_id) ON DELETE CASCADE,
  tenant_id  UUID NOT NULL,   -- denormalized owner for tenant-scoped RLS on the link row
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (mcp_id, tool_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_tools_tool ON flow_tools.mcp_tools (tool_id);

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — mirrors the tool_bindings split-RLS pattern.
-- =====================================================================================

ALTER TABLE flow_tools.tools     ENABLE ROW LEVEL SECURITY;
ALTER TABLE flow_tools.mcps      ENABLE ROW LEVEL SECURITY;
ALTER TABLE flow_tools.mcp_tools ENABLE ROW LEVEL SECURITY;

-- current tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid
-- platform ctx   := NULLIF(current_setting('app.tenant_id', true), '') IS NULL  (empty GUC)

-- ── tools ─────────────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS p_tools_read ON flow_tools.tools;
CREATE POLICY p_tools_read ON flow_tools.tools FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_tools_write ON flow_tools.tools;
CREATE POLICY p_tools_write ON flow_tools.tools FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Platform-context READ ONLY: the UNAUTHENTICATED GET /m/<slug>/manifest endpoint joins an MCP's
-- member tools when app.tenant_id is empty. `protected` cross-tenant grants are a FUTURE join here.
DROP POLICY IF EXISTS p_tools_platform_read ON flow_tools.tools;
CREATE POLICY p_tools_platform_read ON flow_tools.tools FOR SELECT
  USING (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── mcps ──────────────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS p_mcps_read ON flow_tools.mcps;
CREATE POLICY p_mcps_read ON flow_tools.mcps FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_mcps_write ON flow_tools.mcps;
CREATE POLICY p_mcps_write ON flow_tools.mcps FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_mcps_platform_read ON flow_tools.mcps;
CREATE POLICY p_mcps_platform_read ON flow_tools.mcps FOR SELECT
  USING (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── mcp_tools ─────────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS p_mcp_tools_read ON flow_tools.mcp_tools;
CREATE POLICY p_mcp_tools_read ON flow_tools.mcp_tools FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_mcp_tools_write ON flow_tools.mcp_tools;
CREATE POLICY p_mcp_tools_write ON flow_tools.mcp_tools FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_mcp_tools_platform_read ON flow_tools.mcp_tools;
CREATE POLICY p_mcp_tools_platform_read ON flow_tools.mcp_tools FOR SELECT
  USING (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- =====================================================================================
-- GRANTS to the runtime role (flow_tools_user). RLS still applies on top of these.
-- =====================================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON flow_tools.tools     TO flow_tools_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON flow_tools.mcps      TO flow_tools_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON flow_tools.mcp_tools TO flow_tools_user;

-- =====================================================================================
-- DATA MIGRATION — backfill tools + singleton MCPs + links from existing tool_bindings.
-- Runs as the migration role (owner/superuser => RLS bypassed), so no app.tenant_id is set.
-- Idempotent via NOT EXISTS guards; tool_id/mcp_id reuse the source binding_id for stability.
-- =====================================================================================

-- 1) one atomic tool per binding (copy fields; visibility defaults to private).
INSERT INTO flow_tools.tools
    (tool_id, tenant_id, snake_name, display_name, description, input_schema, output_schema,
     node_red_flow_id, http_method, http_path, runtime_id, visibility, access_mode, status,
     version, created_at, updated_at)
SELECT b.binding_id, b.tenant_id, b.snake_name, b.display_name, b.description, b.input_schema,
       b.output_schema, b.node_red_flow_id, b.http_method, b.http_path, b.runtime_id, 'private',
       b.access_mode, b.status, b.version, b.created_at, b.updated_at
FROM flow_tools.tool_bindings b
WHERE NOT EXISTS (SELECT 1 FROM flow_tools.tools t WHERE t.tool_id = b.binding_id);

-- 2) one SINGLETON MCP per binding. server_name = 'tool-<slug>' PRESERVES the existing registry
--    key; slug = the binding's slug so /m/<slug> and the legacy /w/<slug> resolve the same server.
INSERT INTO flow_tools.mcps
    (mcp_id, tenant_id, slug, server_name, display_name, description, visibility, status, version,
     created_at, updated_at)
SELECT b.binding_id, b.tenant_id, b.slug, 'tool-' || b.slug, b.display_name, b.description,
       'private', b.status, b.version, b.created_at, b.updated_at
FROM flow_tools.tool_bindings b
WHERE NOT EXISTS (SELECT 1 FROM flow_tools.mcps m WHERE m.mcp_id = b.binding_id);

-- 3) link each singleton MCP to its one member tool.
INSERT INTO flow_tools.mcp_tools (mcp_id, tool_id, tenant_id, created_at)
SELECT b.binding_id, b.binding_id, b.tenant_id, b.created_at
FROM flow_tools.tool_bindings b
WHERE NOT EXISTS (
    SELECT 1 FROM flow_tools.mcp_tools mt
     WHERE mt.mcp_id = b.binding_id AND mt.tool_id = b.binding_id
);
