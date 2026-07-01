-- =====================================================================================
-- tool-registry — first-cycle schema (WP11). PostgreSQL 16. Idempotent.
--
-- Run as a superuser / migration role. Creates the `tools` schema, the four registry
-- tables, indexes, Row Level Security (Contract 13), and the grants the runtime role
-- `tool_user` needs.
--
-- TABLES (all MIXED-SCOPE: platform rows tenant_id IS NULL + per-tenant rows):
--   tools, tool_versions, tool_capabilities, tool_health
--
-- THE MARKETPLACE HOLE + ITS FIX:
--   A naive RLS policy uses only `USING (tenant_id = current_tenant OR tenant_id IS NULL)`.
--   That guards READS but NOT writes — a tenant could INSERT/UPDATE a row carrying ANOTHER
--   tenant's tenant_id (or NULL to forge a platform tool). We CLOSE this by splitting each
--   table's policy into a permissive SELECT (read own + platform) and a SEPARATE write
--   policy whose `WITH CHECK (tenant_id = current_tenant)` REJECTS any INSERT/UPDATE that
--   names a tenant_id other than the caller's — on EVERY tenant-scoped table, INCLUDING
--   tool_capabilities. The poller/seed paths run with an EMPTY app.tenant_id and are
--   handled by a dedicated platform policy.
--
-- The runtime role connects and runs every tenant-scoped query inside
--   BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT
-- (the Core in_tenant() helper). It is NOT a superuser and does NOT BYPASSRLS.
--
-- RLS uses the pooled-reset-safe NULLIF(current_setting('app.tenant_id',true),'')::uuid
-- so an empty/unset GUC after a pooled-connection reset never throws on the ''::uuid cast.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS tools;

SET search_path = tools, public;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tool_user') THEN
    CREATE ROLE tool_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA tools TO tool_user;

-- =====================================================================================
-- TABLES
-- =====================================================================================

-- ── tools (the tool registry) ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tools.tools (
  tool_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID,                       -- NULL = platform tool
  name           VARCHAR(100) NOT NULL,      -- dash-case MCP server name
  status         VARCHAR(20)  NOT NULL DEFAULT 'active',
  latest_version VARCHAR(40),                -- semver of the newest active version
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- Unique per owner: a tenant cannot register two tools of the same name; platform names
-- are unique among platform tools (NULL tenant_id is "distinct" so a partial index pins it).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tools_tenant_name
  ON tools.tools (tenant_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tools_platform_name
  ON tools.tools (name) WHERE tenant_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_tools_name ON tools.tools (name);

-- ── tool_versions (version chain; retention max 3 active per tool) ────────────────────
CREATE TABLE IF NOT EXISTS tools.tool_versions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID,                          -- NULL = platform; mirrors tools.tenant_id
  tool_id     UUID NOT NULL REFERENCES tools.tools (tool_id) ON DELETE CASCADE,
  version     VARCHAR(40)  NOT NULL,         -- semver
  manifest    JSONB        NOT NULL,         -- Contract-4 MCP manifest
  status      VARCHAR(20)  NOT NULL DEFAULT 'active',  -- active | retired
  created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (tool_id, version)
);
CREATE INDEX IF NOT EXISTS idx_tool_versions_tool ON tools.tool_versions (tool_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_versions_active
  ON tools.tool_versions (tool_id, created_at DESC) WHERE status = 'active';

-- ── tool_capabilities (declared scopes/capabilities per tool) ─────────────────────────
CREATE TABLE IF NOT EXISTS tools.tool_capabilities (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID,                       -- NULL = platform; mirrors tools.tenant_id
  tool_id        UUID NOT NULL REFERENCES tools.tools (tool_id) ON DELETE CASCADE,
  capability     VARCHAR(100) NOT NULL,      -- snake_case tool name (invocable capability)
  required_scope VARCHAR(120) NOT NULL,      -- scope required to invoke it
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (tool_id, capability)
);
CREATE INDEX IF NOT EXISTS idx_tool_caps_tool ON tools.tool_capabilities (tool_id);

-- ── tool_health (manifest poll state) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tools.tool_health (
  tool_id              UUID PRIMARY KEY REFERENCES tools.tools (tool_id) ON DELETE CASCADE,
  tenant_id            UUID,                 -- NULL = platform; mirrors tools.tenant_id
  status               VARCHAR(20) NOT NULL DEFAULT 'active',  -- active | degraded | offline
  last_etag            TEXT,
  consecutive_failures INTEGER     NOT NULL DEFAULT 0,
  last_polled          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tool_health_status ON tools.tool_health (status);

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — corrected SPLIT policy with WITH CHECK.
-- Every tenant-scoped query runs inside a tx that does
--   SELECT set_config('app.tenant_id','<uuid>',true).
-- The background poller / platform seed run with an EMPTY app.tenant_id.
-- =====================================================================================

ALTER TABLE tools.tools             ENABLE ROW LEVEL SECURITY;
ALTER TABLE tools.tool_versions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tools.tool_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE tools.tool_health       ENABLE ROW LEVEL SECURITY;

-- Reusable predicate fragments (documented; inlined per policy below):
--   current tenant  := NULLIF(current_setting('app.tenant_id', true), '')::uuid
--   own row         := tenant_id = current_tenant
--   platform row    := tenant_id IS NULL
--   platform ctx    := NULLIF(current_setting('app.tenant_id', true), '') IS NULL  (empty GUC)

-- ── tools ─────────────────────────────────────────────────────────────────────────────
-- READ: own rows + platform rows (discovery UNION).
DROP POLICY IF EXISTS p_tools_read ON tools.tools;
CREATE POLICY p_tools_read ON tools.tools FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

-- WRITE (tenant): a tenant may INSERT/UPDATE/DELETE ONLY its own rows. The WITH CHECK
-- half is the marketplace-hole fix — it REJECTS a write that names another tenant_id
-- (or NULL to forge a platform tool).
DROP POLICY IF EXISTS p_tools_write ON tools.tools;
CREATE POLICY p_tools_write ON tools.tools FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- WRITE (platform): the seed/poller run with an EMPTY GUC and may manage platform rows
-- (tenant_id IS NULL) ONLY. Still split USING/WITH CHECK so an empty-GUC path can never
-- touch a tenant's row.
DROP POLICY IF EXISTS p_tools_platform ON tools.tools;
CREATE POLICY p_tools_platform ON tools.tools FOR ALL
  USING      (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── tool_versions ─────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS p_tool_versions_read ON tools.tool_versions;
CREATE POLICY p_tool_versions_read ON tools.tool_versions FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_tool_versions_write ON tools.tool_versions;
CREATE POLICY p_tool_versions_write ON tools.tool_versions FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_tool_versions_platform ON tools.tool_versions;
CREATE POLICY p_tool_versions_platform ON tools.tool_versions FOR ALL
  USING      (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── tool_capabilities (own WITH CHECK policy — explicitly required) ───────────────────
DROP POLICY IF EXISTS p_tool_caps_read ON tools.tool_capabilities;
CREATE POLICY p_tool_caps_read ON tools.tool_capabilities FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_tool_caps_write ON tools.tool_capabilities;
CREATE POLICY p_tool_caps_write ON tools.tool_capabilities FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_tool_caps_platform ON tools.tool_capabilities;
CREATE POLICY p_tool_caps_platform ON tools.tool_capabilities FOR ALL
  USING      (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── tool_health ───────────────────────────────────────────────────────────────────────
-- READ: own + platform (discovery shows health). WRITE is platform-context only because
-- the manifest poller (empty GUC) owns the health columns; a tenant never writes health
-- directly (only via registration's _init_health, which runs in the tenant tx — covered by
-- the tenant write policy below).
DROP POLICY IF EXISTS p_tool_health_read ON tools.tool_health;
CREATE POLICY p_tool_health_read ON tools.tool_health FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_tool_health_write ON tools.tool_health;
CREATE POLICY p_tool_health_write ON tools.tool_health FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Platform-context health policy: the poller (empty GUC) updates health for ALL tools,
-- both platform AND tenant-owned. It must therefore be allowed to write tenant rows it
-- does NOT own — but ONLY from the trusted empty-GUC poller context, never from a tenant
-- request (a tenant request always has a non-empty GUC, so this policy never applies to it).
DROP POLICY IF EXISTS p_tool_health_poller ON tools.tool_health;
CREATE POLICY p_tool_health_poller ON tools.tool_health FOR ALL
  USING      (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- =====================================================================================
-- GRANTS to the runtime role (tool_user). RLS still applies on top of these.
-- =====================================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON tools.tools             TO tool_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON tools.tool_versions     TO tool_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON tools.tool_capabilities TO tool_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON tools.tool_health       TO tool_user;
