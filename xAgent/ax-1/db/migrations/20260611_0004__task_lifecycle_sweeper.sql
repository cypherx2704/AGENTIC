-- =====================================================================================
-- xAgent agent-runtime — WP08: task-lifecycle reliability core. PostgreSQL 16.
--
-- Adds the database support the backup sweeper (services/sweeper.py) and retention need:
--
--  1. A SWEEPER RLS bypass for the runtime role. The sweeper runs in-process as a
--     background task with NO per-tenant `app.tenant_id` set — it must DISCOVER stuck,
--     non-terminal tasks ACROSS ALL tenants, then finalise each one (failed) together
--     with its `task.failed` outbox row in ONE transaction. The runtime role
--     (`xagent_user`) is NOT BYPASSRLS, so a tenant-less SELECT on the RLS'd `tasks` /
--     `task_steps` tables returns zero rows. Rather than grant BYPASSRLS (which would
--     remove the tenant guard from the WHOLE role on EVERY query), we add an ADDITIVE,
--     OPT-IN policy that admits rows ONLY inside a transaction that explicitly sets
--     `app.sweeper = 'on'`. Normal task-path transactions never set it, so tenant
--     isolation is unchanged for them; the sweeper sets it transaction-locally
--     (SET LOCAL-equivalent) for its discovery + retention work only.
--
--     PostgreSQL combines multiple PERMISSIVE policies with OR, so this sits alongside
--     the existing `app.tenant_id` isolation policy without weakening it: a query sees a
--     row if (tenant matches) OR (sweeper flag is on). Retention DELETEs on `task_steps`
--     also need it, so the role additionally gets DELETE on `task_steps` (it already has
--     DELETE-equivalent reach on `outbox` for retention via the new grant below).
--
--  2. Retention support: DELETE grants + helper indexes so the sweeper can prune
--     published `outbox` rows (> outbox_retention_days) and old `task_steps`
--     (> task_steps_retention_days) with bounded, index-backed scans.
--
-- Idempotent (CREATE ... IF NOT EXISTS / DROP POLICY IF EXISTS). Safe to re-run.
-- =====================================================================================

-- ── 1) Sweeper opt-in RLS bypass (additive PERMISSIVE policy; OR-combined) ─────────────
-- Admits rows when the sweeper GUC is set 'on' for the transaction. current_setting(...,
-- true) uses missing_ok so a normal (flag-unset) transaction yields NULL -> false -> the
-- policy admits nothing extra, leaving the tenant-isolation policy in full force.
DROP POLICY IF EXISTS xagent_tasks_sweeper ON xagent.tasks;
CREATE POLICY xagent_tasks_sweeper ON xagent.tasks FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

DROP POLICY IF EXISTS xagent_task_steps_sweeper ON xagent.task_steps;
CREATE POLICY xagent_task_steps_sweeper ON xagent.task_steps FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

-- ── 2) Retention grants + indexes ─────────────────────────────────────────────────────
-- task_steps: the sweeper DELETEs old audit rows (retention). The role already had
-- SELECT, INSERT; add DELETE (and UPDATE is not needed). outbox: add DELETE for pruning
-- published rows (it already had SELECT, INSERT, UPDATE).
GRANT DELETE ON xagent.task_steps TO xagent_user;
GRANT DELETE ON xagent.outbox     TO xagent_user;

-- Retention scan support: prune published outbox rows by publish time.
CREATE INDEX IF NOT EXISTS idx_outbox_published_at
  ON xagent.outbox (published_at) WHERE published_at IS NOT NULL;

-- Retention scan support: prune old audit rows by creation time.
CREATE INDEX IF NOT EXISTS idx_task_steps_created_at
  ON xagent.task_steps (created_at);
