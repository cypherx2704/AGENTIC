-- =====================================================================================
-- tool-registry — server-wide DEFAULT access mode for restricted tools.
--
-- Problem: resolve_agent_tool_access() defaulted a restricted tool to 'none' (deny) whenever
-- an agent had no explicit per-agent row. That makes it impossible to publish a tool as
-- "ask" (callable, but each call needs HIL approval) for ALL of a tenant's agents without
-- enumerating every agent up front — so flow-tools published with the default 'ask' posture
-- were silently uncallable by any agent.
--
-- Fix: restricted_tools carries a per-tool DEFAULT access mode. resolve_agent_tool_access()
-- falls back to it (instead of a hardcoded 'none') when the tool is restricted and the agent
-- has no explicit (server[/capability]) row. Backward compatible: the column defaults to
-- 'none', preserving the previous behaviour for every existing restricted tool.
-- =====================================================================================

ALTER TABLE tools.restricted_tools
  ADD COLUMN IF NOT EXISTS default_access_mode VARCHAR(15) NOT NULL DEFAULT 'none';

ALTER TABLE tools.restricted_tools
  DROP CONSTRAINT IF EXISTS restricted_tools_default_mode_chk;
ALTER TABLE tools.restricted_tools
  ADD CONSTRAINT restricted_tools_default_mode_chk
  CHECK (default_access_mode IN ('none', 'ask', 'automated'));
