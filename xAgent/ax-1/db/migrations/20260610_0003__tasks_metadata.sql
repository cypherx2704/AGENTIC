-- =====================================================================================
-- xAgent agent-runtime — WP02: `xagent.tasks.metadata` JSONB column. PostgreSQL 16.
--
-- Persists `TaskRequest.metadata` (free-form caller tags from the request body; the
-- reserved-identity-key list is enforced at the API layer before persistence). The
-- request schema always carried `metadata`; the table did not — codified in the Phase 9
-- Amendment Log (2026-06): "xagent.tasks.metadata JSONB column".
--
-- Idempotent (ADD COLUMN IF NOT EXISTS) — safe to re-run. No RLS/grant changes needed:
-- the existing xagent_tasks_isolation policy and tasks grants cover the new column.
-- =====================================================================================

ALTER TABLE xagent.tasks
  ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';
