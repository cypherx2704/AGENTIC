-- =====================================================================================
-- xagent — orchestrator hierarchy columns on xagent.agents (mirrors auth.agents).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260611_0005.
--
-- xagent.agents holds the RUNTIME config for an agent (llm_model, system_prompt, …). The
-- orchestrator-hierarchy columns let the runtime enforce the immutable-LLM guard (a sub-agent
-- whose `immutable_llm = true` cannot have its `llm_model` overwritten) and surface agent_type /
-- parent in the runtime-config API. The authoritative hierarchy lives in auth.agents; these are
-- denormalised copies kept in sync at runtime-registration time.
-- =====================================================================================

ALTER TABLE xagent.agents
  ADD COLUMN IF NOT EXISTS agent_type VARCHAR(20) NOT NULL DEFAULT 'user_created',
  ADD COLUMN IF NOT EXISTS parent_orchestrator_id UUID,
  ADD COLUMN IF NOT EXISTS immutable_llm BOOLEAN NOT NULL DEFAULT false;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'xagent_agent_type_enum') THEN
    ALTER TABLE xagent.agents
      ADD CONSTRAINT xagent_agent_type_enum
      CHECK (agent_type IN ('orchestrator','sub_agent','user_created'));
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_xagent_agents_parent
  ON xagent.agents (parent_orchestrator_id) WHERE parent_orchestrator_id IS NOT NULL;

-- =====================================================================================
-- end 20260623_0006__orchestrator_model.sql
-- =====================================================================================
