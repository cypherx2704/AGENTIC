-- =====================================================================================
-- skill-registry — per-agent skill access control + restricted skills (Phase 5).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260611_0002.
--
-- skills.agent_skill_access — per (agent, skill server[, capability]) access mode:
--   none      = the agent may never invoke the skill (hard deny).
--   ask       = pause and require a human-in-the-loop approval before each invocation.
--   automated = invoke freely (the default for an unrestricted skill).
-- skills.restricted_skills — skills (e.g. payment) that require explicit user authorization; an agent
--   with no explicit access row defaults to 'none' for a restricted skill (vs 'automated' otherwise).
--
-- RLS follows the registry's marketplace-hole split pattern (read own+platform, write own only).
-- =====================================================================================

-- ── restricted_skills ───────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skills.restricted_skills (
  skill_id    UUID PRIMARY KEY REFERENCES skills.skills(skill_id) ON DELETE CASCADE,
  tenant_id  UUID,                 -- NULL = platform-wide restriction
  reason     TEXT NOT NULL,        -- 'payment' | 'pii' | …
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_restricted_skills_tenant ON skills.restricted_skills (tenant_id);

-- ── agent_skill_access ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skills.agent_skill_access (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID         NOT NULL,
  agent_id         UUID         NOT NULL,
  skill_server_name VARCHAR(100) NOT NULL,
  skill_capability  VARCHAR(100),                 -- NULL = applies to ALL capabilities of the server
  access_mode      VARCHAR(15)  NOT NULL DEFAULT 'automated',
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT agent_skill_access_mode_chk CHECK (access_mode IN ('none','ask','automated')),
  CONSTRAINT agent_skill_access_uq UNIQUE (tenant_id, agent_id, skill_server_name, skill_capability)
);
CREATE INDEX IF NOT EXISTS idx_agent_skill_access_lookup
  ON skills.agent_skill_access (tenant_id, agent_id, skill_server_name);

-- ── RLS ──────────────────────────────────────────────────────────────────────────────────────
ALTER TABLE skills.restricted_skills  ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills.agent_skill_access ENABLE ROW LEVEL SECURITY;

-- restricted_skills: read own + platform; write own only.
DROP POLICY IF EXISTS p_restricted_skills_read ON skills.restricted_skills;
CREATE POLICY p_restricted_skills_read ON skills.restricted_skills FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_restricted_skills_write ON skills.restricted_skills;
CREATE POLICY p_restricted_skills_write ON skills.restricted_skills FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_restricted_skills_platform ON skills.restricted_skills;
CREATE POLICY p_restricted_skills_platform ON skills.restricted_skills FOR ALL
  USING      (tenant_id IS NULL AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- agent_skill_access: strictly tenant-owned.
DROP POLICY IF EXISTS p_agent_skill_access_tenant ON skills.agent_skill_access;
CREATE POLICY p_agent_skill_access_tenant ON skills.agent_skill_access FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON skills.restricted_skills  TO skill_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON skills.agent_skill_access TO skill_user;

-- =====================================================================================
-- end 20260623_0003__skill_access_control.sql
-- =====================================================================================
