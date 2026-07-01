-- =====================================================================================
-- llms — model-alias enhancements + per-agent LLM allowlist.
-- PostgreSQL 16. Idempotent. Apply AFTER 20260614_0007.
--
-- Adds to llms.model_aliases:
--   is_default  — exactly one default alias per tenant (the first alias created becomes it).
--   task_type   — what the alias is FOR (fast-response | code-generation | analysis | …), so the
--                 orchestrator can pick an appropriate model per sub-agent task.
--   description — human-readable note.
--
-- Adds llms.agent_allowed_llm_aliases — an allowlist restricting which aliases a given agent (or
-- the orchestrator) may use. An EMPTY allowlist for an agent = unrestricted (any resolvable alias).
-- =====================================================================================

ALTER TABLE llms.model_aliases
  ADD COLUMN IF NOT EXISTS is_default  BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS task_type   VARCHAR(50),
  ADD COLUMN IF NOT EXISTS description TEXT;

-- At most one default alias per tenant scope (NULL tenant = platform). Partial unique index treats
-- each tenant (and the platform NULL bucket) independently.
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_aliases_default_per_tenant
  ON llms.model_aliases (COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE is_default = true;

-- First alias inserted in a tenant scope auto-becomes the default (matches the product rule
-- "the first alias created will be default"). A later explicit PATCH can re-point the default.
CREATE OR REPLACE FUNCTION llms.set_first_alias_default() RETURNS trigger AS $$
BEGIN
  IF NOT NEW.is_default AND NOT EXISTS (
    SELECT 1 FROM llms.model_aliases
     WHERE tenant_id IS NOT DISTINCT FROM NEW.tenant_id
       AND is_default = true
       AND id <> NEW.id
  ) THEN
    NEW.is_default := true;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_first_alias_default ON llms.model_aliases;
CREATE TRIGGER trg_set_first_alias_default
  BEFORE INSERT ON llms.model_aliases
  FOR EACH ROW EXECUTE FUNCTION llms.set_first_alias_default();

-- ── per-agent LLM allowlist (tenant-scoped RLS) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llms.agent_allowed_llm_aliases (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  UUID        NOT NULL,
  agent_id   UUID        NOT NULL,
  alias      VARCHAR(50) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, agent_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_agent_llm_aliases_lookup
  ON llms.agent_allowed_llm_aliases (tenant_id, agent_id);

ALTER TABLE llms.agent_allowed_llm_aliases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_agent_llm_aliases_tenant ON llms.agent_allowed_llm_aliases;
CREATE POLICY p_agent_llm_aliases_tenant ON llms.agent_allowed_llm_aliases FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON llms.agent_allowed_llm_aliases TO llms_user;

-- =====================================================================================
-- end 20260623_0009__llm_alias_enhancements.sql
-- =====================================================================================
