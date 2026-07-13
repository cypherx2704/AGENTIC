-- =====================================================================================
-- tool-flow-bridge — cross-tenant PUBLIC-read for promoted (public) MCPs. PostgreSQL 16.
-- Idempotent (safe to re-run). Follows the split-RLS style of 20260712_0004/_0005/_0006.
--
-- PROBLEM (Phase 5 cross-tenant Public-execution gap): POST /v1/mcps/{id}/promote registers a
-- PUBLIC MCP in the registry (tenant_id NULL) and re-homes its member flows onto the SINGLETON
-- platform Node-RED runtime, but the flow_tools.mcps / tools / mcp_tools rows KEEP their owner's
-- tenant_id. The invoke wire POST /m/<slug>/mcp resolves the MCP inside
-- db_pool.in_tenant(callingTenant, ...) -> queries.get_mcp_with_members(slug). Under the 0004
-- policies a DIFFERENT tenant's agent can only SELECT its OWN rows (or, in empty-GUC platform
-- context, all rows), so a foreign tenant's resolve of the owner's public MCP returns nothing and
-- the call 404s. Decision: "all tenants invoke the platform runtime" — Public must be cross-tenant
-- invocable. The platform runtime sentinel row is already readable in any context (0006); ONLY the
-- mcp / tool / membership reads are still missing, which this migration adds.
--
-- FIX — ADDITIVE public-read SELECT policies. RLS is permissive-OR, so each new policy ONLY WIDENS
-- reads: the existing own-tenant (_read) and empty-GUC (_platform_read) policies stay untouched and
-- keep working; these simply also admit rows of a PUBLIC MCP in ANY tenant context. `visibility`
-- is the public-invoke READ boundary here. WRITES are UNCHANGED — the _write policies (own-only,
-- and mcp_tools' owned-tool-AND-owned-mcp WITH CHECK from 0005) are not touched, so a foreign
-- tenant can read a public MCP's rows but can NEVER mutate them; only the owner writes.
--
-- Only visibility='public' rows are exposed. Promote is the sole path to visibility='public'
-- (publisher.promote_mcp, which also flips each member tool to visibility='public' in the same
-- commit txn so the tools _public_read policy admits them). Private/protected rows remain
-- own-tenant-only. No secret material lives in these tables; the member-tool -> tenant_runtimes
-- join resolves through the platform SENTINEL runtime row (0006), never a real tenant's runtime.
-- =====================================================================================

SET search_path = flow_tools, public;

-- ── mcps: any tenant context may SELECT a PUBLIC MCP row ──────────────────────────────
DROP POLICY IF EXISTS p_mcps_public_read ON flow_tools.mcps;
CREATE POLICY p_mcps_public_read ON flow_tools.mcps FOR SELECT
  USING (visibility = 'public');

-- ── tools: any tenant context may SELECT a PUBLIC tool row (promote flips members public) ─
DROP POLICY IF EXISTS p_tools_public_read ON flow_tools.tools;
CREATE POLICY p_tools_public_read ON flow_tools.tools FOR SELECT
  USING (visibility = 'public');

-- ── mcp_tools: any tenant context may SELECT a link row whose MCP is PUBLIC ────────────
DROP POLICY IF EXISTS p_mcp_tools_public_read ON flow_tools.mcp_tools;
CREATE POLICY p_mcp_tools_public_read ON flow_tools.mcp_tools FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM flow_tools.mcps m
       WHERE m.mcp_id = mcp_tools.mcp_id
         AND m.visibility = 'public'
    )
  );
