-- =====================================================================================
-- tool-registry — fix agent_tool_access NULL-capability upsert (2026-06-30).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260623_0003.
--
-- The original UNIQUE (tenant_id, agent_id, tool_server_name, tool_capability) treats a
-- NULL tool_capability (a SERVER-WIDE access rule) as DISTINCT per Postgres NULL semantics,
-- so a server-wide PUT /tools/{name}/access could never UPDATE an existing server-wide row
-- via ON CONFLICT — it inserted a DUPLICATE and resolve() then returned a stale access mode
-- (e.g. flipping none -> automated appeared to have no effect). Replace the constraint with a
-- COALESCE(tool_capability,'') unique index so a NULL capability collapses to one canonical
-- key; the upsert (ON CONFLICT ... COALESCE(tool_capability,'')) now updates in place.
--
-- (Same fix shipped for the mirrored skill-registry in Skills/skill-registry 20260624_0004.)
-- =====================================================================================

ALTER TABLE tools.agent_tool_access DROP CONSTRAINT IF EXISTS agent_tool_access_uq;

-- Collapse any DUPLICATE rows the buggy upsert may have created (keep the most-recently
-- updated row per canonical key) so the unique index below can be built.
DELETE FROM tools.agent_tool_access a
 USING tools.agent_tool_access b
 WHERE a.tenant_id = b.tenant_id
   AND a.agent_id = b.agent_id
   AND a.tool_server_name = b.tool_server_name
   AND COALESCE(a.tool_capability, '') = COALESCE(b.tool_capability, '')
   AND (a.updated_at < b.updated_at OR (a.updated_at = b.updated_at AND a.ctid < b.ctid));

CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_tool_access
  ON tools.agent_tool_access (tenant_id, agent_id, tool_server_name, COALESCE(tool_capability, ''));
