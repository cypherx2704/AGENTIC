-- =====================================================================================
-- skill-registry — fix agent_skill_access NULL-capability upsert (Phase 8, 2026-06-29).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260624_0003.
--
-- The original UNIQUE (tenant_id, agent_id, skill_server_name, skill_capability) treats a
-- NULL skill_capability (a SERVER-WIDE access rule) as DISTINCT per Postgres NULL semantics,
-- so a server-wide PUT /skills/{name}/access could never UPDATE an existing server-wide row
-- via ON CONFLICT — it inserted a DUPLICATE and resolve() then returned a stale access mode
-- (e.g. flipping none -> automated appeared to have no effect). Replace the constraint with a
-- COALESCE(skill_capability,'') unique index so a NULL capability collapses to one canonical
-- key; the upsert (ON CONFLICT ... COALESCE(skill_capability,'')) now updates in place.
--
-- NOTE: the mirrored tool-registry (tools.agent_tool_access) has the SAME latent bug.
-- =====================================================================================

ALTER TABLE skills.agent_skill_access DROP CONSTRAINT IF EXISTS agent_skill_access_uq;

-- Collapse any DUPLICATE rows the buggy upsert may have created (keep the most-recently
-- updated row per canonical key) so the unique index below can be built.
DELETE FROM skills.agent_skill_access a
 USING skills.agent_skill_access b
 WHERE a.tenant_id = b.tenant_id
   AND a.agent_id = b.agent_id
   AND a.skill_server_name = b.skill_server_name
   AND COALESCE(a.skill_capability, '') = COALESCE(b.skill_capability, '')
   AND (a.updated_at < b.updated_at OR (a.updated_at = b.updated_at AND a.ctid < b.ctid));

CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_skill_access
  ON skills.agent_skill_access (tenant_id, agent_id, skill_server_name, COALESCE(skill_capability, ''));
