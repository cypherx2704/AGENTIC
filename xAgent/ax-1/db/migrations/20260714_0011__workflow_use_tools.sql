-- 0011 — RUN-level tool switch: xagent.workflows.use_tools.
--
-- WHAT IT IS
-- ----------
-- The caller's "Use Tools" choice for ONE run, set at POST /v1/orchestrations and surfaced next to
-- the prompt box. FALSE means every task in the run — the orchestrator's own and every sub-agent's —
-- runs as a plain chat completion: TOOL_LOOP does not resolve, offer, or invoke a single tool, and
-- the planner is shown a roster with no tools, so it cannot route a step to a capability that is
-- switched off. TRUE (the default, and every existing run) is today's behaviour: the LLM sees the
-- tools an agent holds and decides for itself whether to call them.
--
-- WHY A COLUMN AND NOT A REQUEST-SCOPED FLAG
-- -----------------------------------------
-- `mode` (solo|subagents) is already persisted here and this is the same kind of thing: a property
-- of the RUN, not of a process. Persisting it means GET /v1/orchestrations/{id} can report how a run
-- was configured, the audit trail explains why a run made no tool calls, and the flag survives if
-- driving ever moves to a worker that re-reads the row instead of holding it in memory.
--
-- WHY IT IS SEPARATE FROM agents.tool_loop_enabled (0007)
-- ------------------------------------------------------
-- 0007 is an AGENT-level toggle: "this agent never runs the loop" (a standing config choice, e.g. a
-- rate-limited free-tier model). This is a RUN-level toggle: "not on THIS run" (a per-prompt user
-- choice). They are ANDed — either one alone switches tools off — so neither can silently re-enable
-- what the other turned off.
--
-- NOT NULL DEFAULT true backfills every existing row to the prior behaviour, so the migration is a
-- no-op for anything already in flight. No new RLS policies or grants: the existing workflow
-- isolation policy + grants cover the new column. Idempotent.
ALTER TABLE xagent.workflows
  ADD COLUMN IF NOT EXISTS use_tools BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN xagent.workflows.use_tools IS
  'RUN-level tool switch. false => every task in this run is a plain chat completion: TOOL_LOOP is '
  'skipped (no tool resolved, offered or invoked) and the planner sees a toolless roster. Distinct '
  'from agents.tool_loop_enabled (an agent-level standing toggle); the two are ANDed.';
