-- =====================================================================================
-- tool-flow-bridge — platform (public) Node-RED runtime. PostgreSQL 16. Idempotent.
--
-- Phase 5 (5-bridge): a SINGLETON, platform-owned Node-RED runtime, distinct from the
-- per-tenant (egress-DENY) runtimes. It hosts PUBLIC tools (promoted MCPs), egresses to the
-- external search providers (egress-ALLOW), and holds a platform provider-key credential.
--
-- SCHEMA CHOICE — a SENTINEL ROW in flow_tools.tenant_runtimes (NOT a new table):
--   flow_tools.tools.runtime_id has a FK -> flow_tools.tenant_runtimes(runtime_id). Re-homing a
--   promoted MCP's member tools repoints tools.runtime_id at the platform runtime, so the platform
--   runtime MUST be a tenant_runtimes row for that FK to hold. A dedicated `platform_runtime` table
--   would break that FK (or force a UNION into every runtime join). Reusing tenant_runtimes with a
--   well-known SENTINEL tenant_id (the nil UUID — never a real tenant) keeps the FK + every existing
--   runtime join / upsert / secret-resolution path intact and needs zero new plumbing. The row is
--   provisioned + upserted on demand by services.provisioner.ensure_platform_runtime (in_platform).
--
-- RLS — the platform runtime is SHARED infrastructure hosting public tools every tenant may invoke,
--   so its (sentinel) row is READABLE in every context (platform + any tenant): needed so a public
--   MCP's member-tool -> tenant_runtimes join resolves both for the invoking tenant AND for the owner
--   after its tools are re-homed onto the platform runtime. It stays WRITABLE only in platform
--   (empty-GUC) context. This is a deliberate, NARROW exception to 0003 (which removed platform
--   access to tenant_runtimes): it admits ONLY the sentinel row — never any real tenant's runtime —
--   so there is NO cross-tenant runtime-secret-ref leak, and the columns hold only secret *refs*
--   (never material) anyway. Real tenant runtimes remain own-tenant-only.
--
-- The sentinel tenant_id below is kept in sync with
--   services.provisioner.PLATFORM_RUNTIME_TENANT_ID.
-- =====================================================================================

SET search_path = flow_tools, public;

-- ── platform runtime: read the sentinel row in ANY context (shared public infrastructure) ─────
DROP POLICY IF EXISTS p_tenant_runtimes_platform_read ON flow_tools.tenant_runtimes;
CREATE POLICY p_tenant_runtimes_platform_read ON flow_tools.tenant_runtimes FOR SELECT
  USING (tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);

-- ── platform runtime: write (provision/upsert) the sentinel row ONLY in platform (empty-GUC) ctx ─
DROP POLICY IF EXISTS p_tenant_runtimes_platform_write ON flow_tools.tenant_runtimes;
CREATE POLICY p_tenant_runtimes_platform_write ON flow_tools.tenant_runtimes FOR ALL
  USING      (NULLIF(current_setting('app.tenant_id', true), '') IS NULL
              AND tenant_id = '00000000-0000-0000-0000-000000000000'::uuid)
  WITH CHECK (NULLIF(current_setting('app.tenant_id', true), '') IS NULL
              AND tenant_id = '00000000-0000-0000-0000-000000000000'::uuid);
