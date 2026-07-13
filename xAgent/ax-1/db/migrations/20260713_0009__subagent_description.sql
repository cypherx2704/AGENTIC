-- 0009 — sub-agent ROUTING DESCRIPTION.
--
-- The orchestrator's planner routes a step to a sub-agent. To do that it must know what each
-- sub-agent is FOR. Until now the only purpose signal was `system_prompt`, which is the wrong
-- text for the job on two counts:
--
--   * it addresses the AGENT ("You are terse. Always cite sources."), not the ROUTER
--     ("Use me to fetch GitHub repository statistics."); and
--   * it is optional in practice — the UI defaulted it to "You are a helpful assistant. Answer
--     concisely.", so every sub-agent advertised an identical, routing-useless purpose and the
--     planner was left guessing from the agent's NAME.
--
-- `description` is that missing advertisement: a one-line "when to use this agent", written for
-- the planner. It is rendered into the planner's capability catalogue alongside the agent's real
-- `allowed_tools`, so a step is routed on INTENT (description) and constrained by CAPABILITY
-- (tools) rather than on a name string.
--
-- Backfilled to '' and NOT NULL DEFAULT '' so existing rows are valid immediately; the roster
-- reader falls back to `system_prompt` when `description` is empty, which preserves the previous
-- behaviour for agents created before this migration.
ALTER TABLE xagent.agents
  ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN xagent.agents.description IS
  'Routing description ("when to use this agent") shown to the orchestrator''s planner. Distinct '
  'from system_prompt, which is the agent''s own instructions. Empty => the roster falls back to '
  'system_prompt.';
