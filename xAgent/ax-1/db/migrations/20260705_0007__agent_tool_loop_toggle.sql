-- =====================================================================================
-- xagent — per-agent tool-loop toggle on xagent.agents. PostgreSQL 16. Idempotent.
-- Apply AFTER 20260623_0006.
--
-- Adds ``tool_loop_enabled`` — a per-agent switch controlling whether the TOOL_LOOP stage
-- runs for this agent (i.e. whether a task may make MULTIPLE LLM<->tool round-trips) or is
-- SKIPPED so the task makes exactly ONE LLM call (the base LLM answer stands).
--
--   * tool_loop_enabled = true  (DEFAULT) — "multiple request": the full bounded tool loop
--     runs, up to ``TOOL_LOOP_MAX_ITERATIONS`` (current behaviour for every agent with
--     ``allowed_tools``). A toolless agent is single-call regardless (the stage already
--     skips with no allowed_tools), so this default preserves EVERY existing agent's
--     behaviour byte-for-byte — zero regression.
--   * tool_loop_enabled = false — "per request": the TOOL_LOOP stage skips even when the
--     agent has ``allowed_tools``, so the task makes a single LLM call. Intended for
--     rate-limited / free-tier models where multiple round-trips exhaust the provider's
--     shared usage limit.
--
-- Enforcement is one guard at the top of the TOOL_LOOP stage (alongside the existing
-- ``if not agent.allowed_tools`` skip) — the default served pipeline shape is unchanged.
--
-- NOT NULL DEFAULT true backfills existing rows to the prior behaviour. Idempotent
-- (ADD COLUMN IF NOT EXISTS). Safe to re-run. No new RLS policies or grants: the existing
-- agents isolation policy + grants cover the new column.
-- =====================================================================================

ALTER TABLE xagent.agents
  ADD COLUMN IF NOT EXISTS tool_loop_enabled BOOLEAN NOT NULL DEFAULT true;

-- =====================================================================================
-- end 20260705_0007__agent_tool_loop_toggle.sql
-- =====================================================================================
