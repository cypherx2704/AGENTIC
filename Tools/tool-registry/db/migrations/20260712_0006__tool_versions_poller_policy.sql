-- Migration 0006: let the manifest health-poller (empty-GUC context) refresh the cached
-- manifest of TENANT-owned tool versions IN PLACE — enabling automatic manifest propagation
-- without a re-publish or a tool-version bump.
--
-- Why: services/health_runner.poll_one -> db/queries.update_health runs under in_platform
-- (empty app.tenant_id GUC) and, on a changed 200 from a tool server's /manifest, UPDATEs
-- tools.tool_versions.manifest of the latest active version row (the column discovery serves).
-- tool_versions previously had only:
--   * p_tool_versions_write    (own tenant: tenant_id = current GUC), and
--   * p_tool_versions_platform (platform: tenant_id IS NULL AND empty GUC).
-- Under the empty-GUC poller context, p_tool_versions_write's predicate is NULL/false and
-- p_tool_versions_platform only matches tenant_id IS NULL, so the poller's UPDATE silently
-- matched ZERO rows for TENANT-owned tools (e.g. flow-tools published via tool-flow-bridge).
-- Result: those tools never got manifest refreshes from the poll — discovery kept serving the
-- manifest captured at registration until a new version was explicitly registered.
--
-- Fix: add a poller policy MIRRORING p_tool_health_poller (init migration) — it permits writes
-- ONLY from the trusted empty-GUC poller context. A tenant request ALWAYS carries a non-empty
-- app.tenant_id GUC, so this policy can never apply to a tenant request; it only opens the
-- narrow poller path. Scoped to FOR UPDATE (least privilege): the poller only refreshes the
-- manifest column of an existing row — it never INSERTs or DELETEs versions (those stay
-- governed by the tenant / platform write policies at registration). Idempotent.

DROP POLICY IF EXISTS p_tool_versions_poller ON tools.tool_versions;
CREATE POLICY p_tool_versions_poller ON tools.tool_versions FOR UPDATE
  USING      (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);
