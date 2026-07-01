-- =====================================================================================
-- auth-service — Human-in-the-Loop (HIL) approval framework (Phase 6).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260623_0011 (or 0010 if 0011 was folded in).
--
-- Extends the existing auth.approval_requests (step-up approvals) with agent-operation context, and
-- adds auth.orchestrator_hil_config — the per-orchestrator mode that decides whether an `ask`-mode
-- action actually pauses for a human:
--   automated      — never pause (ask-mode tools auto-proceed).
--   human_in_loop  — every gated action pauses for approval.
--   partial        — only operation types listed in ask_on_triggers pause.
-- =====================================================================================

ALTER TABLE auth.approval_requests
  ADD COLUMN IF NOT EXISTS operation_type    VARCHAR(30),
  -- tool_execution | sub_agent_creation | llm_restriction | skill_execution
  ADD COLUMN IF NOT EXISTS operation_context JSONB NOT NULL DEFAULT '{}';

-- approval_requests already enforces RLS (USING tenant_id = app.tenant_id) from the init migration;
-- the new columns inherit it. Loosen the legacy NOT NULL on `scopes` so a HIL operation-approval
-- (which is not scope-based) can omit it.
ALTER TABLE auth.approval_requests ALTER COLUMN scopes DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_approval_requests_operation
  ON auth.approval_requests (tenant_id, operation_type, status)
  WHERE status = 'pending';

-- ── orchestrator_hil_config ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth.orchestrator_hil_config (
  agent_id        UUID PRIMARY KEY REFERENCES auth.agents(agent_id) ON DELETE CASCADE,
  tenant_id       UUID NOT NULL,
  default_mode    VARCHAR(20) NOT NULL DEFAULT 'automated',
  ask_on_triggers TEXT[]      NOT NULL DEFAULT '{}',
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT orch_hil_mode_chk CHECK (default_mode IN ('automated','human_in_loop','partial'))
);
CREATE INDEX IF NOT EXISTS idx_orch_hil_config_tenant ON auth.orchestrator_hil_config (tenant_id);

ALTER TABLE auth.orchestrator_hil_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_orch_hil_config_tenant ON auth.orchestrator_hil_config;
CREATE POLICY p_orch_hil_config_tenant ON auth.orchestrator_hil_config
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.orchestrator_hil_config TO auth_user;

-- =====================================================================================
-- end 20260623_0012__hil_framework.sql
-- =====================================================================================
