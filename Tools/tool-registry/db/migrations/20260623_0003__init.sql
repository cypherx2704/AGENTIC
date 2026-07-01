-- =====================================================================================
-- tool-registry — per-agent tool access control + restricted tools (Phase 5).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260611_0002.
--
-- tools.agent_tool_access — per (agent, tool server[, capability]) access mode:
--   none      = the agent may never invoke the tool (hard deny).
--   ask       = pause and require a human-in-the-loop approval before each invocation.
--   automated = invoke freely (the default for an unrestricted tool).
-- tools.restricted_tools — tools (e.g. payment) that require explicit user authorization; an agent
--   with no explicit access row defaults to 'none' for a restricted tool (vs 'automated' otherwise).
--
-- RLS follows the registry's marketplace-hole split pattern (read own+platform, write own only).
-- =====================================================================================

-- ── restricted_tools ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tools.restricted_tools (
  tool_id    UUID PRIMARY KEY REFERENCES tools.tools(tool_id) ON DELETE CASCADE,
  tenant_id  UUID,                 -- NULL = platform-wide restriction
  reason     TEXT NOT NULL,        -- 'payment' | 'pii' | …
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_restricted_tools_tenant ON tools.restricted_tools (tenant_id);

-- ── agent_tool_access ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tools.agent_tool_access (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID         NOT NULL,
  agent_id         UUID         NOT NULL,
  tool_server_name VARCHAR(100) NOT NULL,
  tool_capability  VARCHAR(100),                 -- NULL = applies to ALL capabilities of the server
  access_mode      VARCHAR(15)  NOT NULL DEFAULT 'automated',
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT agent_tool_access_mode_chk CHECK (access_mode IN ('none','ask','automated')),
  CONSTRAINT agent_tool_access_uq UNIQUE (tenant_id, agent_id, tool_server_name, tool_capability)
);
CREATE INDEX IF NOT EXISTS idx_agent_tool_access_lookup
  ON tools.agent_tool_access (tenant_id, agent_id, tool_server_name);

-- ── RLS ──────────────────────────────────────────────────────────────────────────────────────
ALTER TABLE tools.restricted_tools  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tools.agent_tool_access ENABLE ROW LEVEL SECURITY;

-- restricted_tools: read own + platform; write own only.
DROP POLICY IF EXISTS p_restricted_tools_read ON tools.restricted_tools;
CREATE POLICY p_restricted_tools_read ON tools.restricted_tools FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_restricted_tools_write ON tools.restricted_tools;
CREATE POLICY p_restricted_tools_write ON tools.restricted_tools FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_restricted_tools_platform ON tools.restricted_tools;
CREATE POLICY p_restricted_tools_platform ON tools.restricted_tools FOR ALL
  USING      (tenant_id IS NULL AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- agent_tool_access: strictly tenant-owned.
DROP POLICY IF EXISTS p_agent_tool_access_tenant ON tools.agent_tool_access;
CREATE POLICY p_agent_tool_access_tenant ON tools.agent_tool_access FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON tools.restricted_tools  TO tool_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON tools.agent_tool_access TO tool_user;

-- =====================================================================================
-- end 20260623_0003__tool_access_control.sql
-- =====================================================================================
