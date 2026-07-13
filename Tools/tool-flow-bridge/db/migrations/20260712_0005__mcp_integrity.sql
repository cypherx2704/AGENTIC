-- =====================================================================================
-- tool-flow-bridge — integrity hardening for the atomic-tool + MCP-collection model.
-- PostgreSQL 16. Idempotent (safe to re-run). Follows 20260712_0004's split-RLS style.
--
-- Closes two Phase-1 findings now that Phase 2 makes flow_tools.tools/mcps/mcp_tools the
-- source of truth for the publish/MCP-management path:
--
--   * FINDING #5 — value integrity: add CHECK (status IN ('active','retired')) to
--     flow_tools.tools and flow_tools.mcps so no path (bug, direct SQL, future writer) can
--     persist an out-of-domain status (the invoke/manifest wires only ever resolve 'active').
--
--   * FINDING #3 — membership integrity: strengthen the flow_tools.mcp_tools WRITE policy's
--     WITH CHECK so a link row can be written ONLY when BOTH the referenced tool AND the
--     referenced MCP are tenant-owned — not merely when the denormalized mcp_tools.tenant_id
--     column matches. This is the storage backstop under the app-layer ownership validation in
--     POST /v1/mcps + PUT /v1/mcps/{id} (set_mcp_members): even if the app check is bypassed, a
--     tenant can never link a foreign tool_id (or into a foreign mcp_id) into one of its MCPs.
--     The USING clause is unchanged (own rows); only WITH CHECK is tightened.
-- =====================================================================================

SET search_path = flow_tools, public;

-- =====================================================================================
-- FINDING #5 — status domain CHECK on tools + mcps.
-- DROP-then-ADD so re-running is idempotent even when the constraint already exists.
-- Existing rows only ever hold 'active'/'retired', so ADD validates cleanly.
-- =====================================================================================

ALTER TABLE flow_tools.tools DROP CONSTRAINT IF EXISTS tools_status_chk;
ALTER TABLE flow_tools.tools
  ADD CONSTRAINT tools_status_chk CHECK (status IN ('active', 'retired'));

ALTER TABLE flow_tools.mcps DROP CONSTRAINT IF EXISTS mcps_status_chk;
ALTER TABLE flow_tools.mcps
  ADD CONSTRAINT mcps_status_chk CHECK (status IN ('active', 'retired'));

-- =====================================================================================
-- FINDING #3 — mcp_tools WRITE policy: require a tenant-owned tool AND a tenant-owned MCP.
--
-- The 0004 policy only checked mcp_tools.tenant_id = current tenant, so a caller who supplied
-- a foreign tool_id/mcp_id together with its OWN tenant_id in the denormalized column could
-- have slipped a cross-tenant link past RLS. The EXISTS clauses below run under the current
-- tenant GUC (tools/mcps RLS scopes them to own rows too), so the link is admitted only when
-- both endpoints are genuinely owned by the writing tenant. current tenant :=
-- NULLIF(current_setting('app.tenant_id', true), '')::uuid.
-- =====================================================================================

DROP POLICY IF EXISTS p_mcp_tools_write ON flow_tools.mcp_tools;
CREATE POLICY p_mcp_tools_write ON flow_tools.mcp_tools FOR ALL
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND EXISTS (
      SELECT 1 FROM flow_tools.tools t
       WHERE t.tool_id = mcp_tools.tool_id
         AND t.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    )
    AND EXISTS (
      SELECT 1 FROM flow_tools.mcps m
       WHERE m.mcp_id = mcp_tools.mcp_id
         AND m.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    )
  );
