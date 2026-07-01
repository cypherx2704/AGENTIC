-- =====================================================================================
-- llms — tenant-defined LLM rules (the "ultimate truth" only the user can change).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260623_0009.
--
-- A tenant owner can allow/block specific (provider, model) pairs, mark whether agents may use a
-- model, mark a model as user-added (so its usage is NOT billed), and toggle billing_bypass. These
-- rules are enforced in the gateway BEFORE alias resolution. Only tenant:admin may write them.
-- =====================================================================================

CREATE TABLE IF NOT EXISTS llms.user_llm_rules (
  rule_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID         NOT NULL,
  provider              VARCHAR(50)  NOT NULL,
  model_id              VARCHAR(100) NOT NULL,
  rule_type             VARCHAR(10)  NOT NULL DEFAULT 'allow',
  can_be_used_by_agents BOOLEAN      NOT NULL DEFAULT true,
  is_user_added         BOOLEAN      NOT NULL DEFAULT true,
  billing_bypass        BOOLEAN      NOT NULL DEFAULT false,
  created_by            UUID         NOT NULL,
  created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT user_llm_rules_type_chk CHECK (rule_type IN ('allow','block')),
  UNIQUE (tenant_id, provider, model_id)
);
CREATE INDEX IF NOT EXISTS idx_user_llm_rules_tenant ON llms.user_llm_rules (tenant_id);

ALTER TABLE llms.user_llm_rules ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_user_llm_rules_tenant ON llms.user_llm_rules;
CREATE POLICY p_user_llm_rules_tenant ON llms.user_llm_rules FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON llms.user_llm_rules TO llms_user;

-- =====================================================================================
-- end 20260623_0010__user_llm_rules.sql
-- =====================================================================================
