-- =====================================================================================
-- tool-registry — DECOMMISSION the platform `tool-web-search` seed (2026-07-12).
-- PostgreSQL 16. Idempotent. Apply AFTER 20260712_0007.
--
-- WHY: the bespoke `tool-web-search` MCP service is retired. Its capability is now served by
-- the PUBLIC `web_search` flow-tool (MCP server `mcp-web-search`), bootstrapped into the
-- registry by tool-flow-bridge — NOT by this service's startup seed. The platform seed shipped
-- in 20260611_0002 must therefore be removed. Migrations are append-only, so this is a FORWARD
-- decommission migration rather than an edit of history (0002 stays as it shipped).
--
-- CUTOVER (operator ordering — see Tools/tool-flow-bridge/docs/web-search-public-tool.md):
--   1. Deploy the platform runtime + bootstrap the `web_search` Public flow-tool.
--   2. Verify a cross-tenant agent can discover + invoke `web_search`.
--   3. ONLY THEN apply this migration (and ship the tool-web-search removal) so web search is
--      never unavailable during the cutover.
--
-- Deleting the platform `tools` row CASCADES to its `tool_versions`, `tool_capabilities`,
-- `tool_health` and any `restricted_tools` rows (all FK `tool_id` ON DELETE CASCADE).
-- `agent_tool_access` references the server by NAME (no FK), so its now-dangling rows are
-- cleaned explicitly. Re-running is a no-op (DELETE of absent rows).
-- =====================================================================================

SET search_path = tools, public;

-- Dangling per-agent access rows that named the retired server (no FK to cascade them).
DELETE FROM tools.agent_tool_access WHERE tool_server_name = 'tool-web-search';

-- The platform tool (tenant_id IS NULL) + CASCADE (versions, capabilities, health, restrictions).
DELETE FROM tools.tools WHERE name = 'tool-web-search' AND tenant_id IS NULL;
