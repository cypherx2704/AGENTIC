-- =====================================================================================
-- tool-registry — tenant-level VISIBILITY label on tools.tools (Marketplace sectioning).
--
-- Goal: the registry can label/filter tools by visibility so the Marketplace can section
-- them (Public / Private / Protected) WITHOUT changing the platform/tenant RLS model.
--
--   * private   — owner tenant only (the default for a freshly-registered tenant tool).
--   * protected — owner + explicit cross-tenant grants. The grant LOGIC is a FUTURE
--                 extension (see note below); for now `protected` behaves exactly like
--                 `private` (owner-only) — it carries no extra read access yet.
--   * public    — visible to all tenants. Public rows are EXACTLY today's platform rows
--                 (tenant_id IS NULL) — we reuse that mechanism, we do NOT invent a
--                 parallel one. The backfill below marks every existing platform row public.
--
-- IMPORTANT: this migration does NOT weaken RLS. The existing `_read` (own ∪ platform),
-- `_write` (own WITH CHECK own) and `_platform` (empty-GUC, tenant_id IS NULL) policies on
-- tools.tools from the init migration are left exactly as they are — visibility is a label
-- the API filters on, never a security boundary. Idempotent (safe to re-run).
-- =====================================================================================

SET search_path = tools, public;

-- ── visibility column (NOT NULL, defaults to the most-restrictive `private`) ──────────
ALTER TABLE tools.tools
  ADD COLUMN IF NOT EXISTS visibility VARCHAR(15) NOT NULL DEFAULT 'private';

-- CHECK added separately (DROP-then-ADD) so re-running is idempotent even when the column
-- already exists and the inline ADD COLUMN above is skipped.
ALTER TABLE tools.tools
  DROP CONSTRAINT IF EXISTS tools_visibility_chk;
ALTER TABLE tools.tools
  ADD CONSTRAINT tools_visibility_chk
  CHECK (visibility IN ('private', 'protected', 'public'));

-- ── Backfill: platform rows (tenant_id IS NULL) ARE the public rows ───────────────────
-- Existing tenant rows stay `private` (the column default); only platform rows flip public.
UPDATE tools.tools SET visibility = 'public' WHERE tenant_id IS NULL AND visibility <> 'public';

-- ── GOVERNANCE BACKSTOP: `public` is reserved for platform rows (tenant_id IS NULL) ───
-- Public tools are created ONLY by platform promotion, never by a tenant's own registration.
-- The API (_resolve_visibility) rejects a tenant-declared `public`; this CHECK is the storage
-- backstop so no path (bug, direct SQL, future writer) can persist a tenant-owned public row.
-- Added AFTER the backfill so every existing row already satisfies it. DROP-then-ADD = idempotent.
ALTER TABLE tools.tools
  DROP CONSTRAINT IF EXISTS tools_public_is_platform_chk;
ALTER TABLE tools.tools
  ADD CONSTRAINT tools_public_is_platform_chk
  CHECK (visibility <> 'public' OR tenant_id IS NULL);

-- =====================================================================================
-- FUTURE EXTENSION POINT (not built here): `protected` cross-tenant reads.
-- When protected grants land, a grantee tenant will see a protected tool it was granted via
-- a cross-tenant join (e.g. a `tool_grants(tool_id, grantee_tenant_id)` table) added to the
-- `_read` policy's USING clause as an extra OR branch:
--     ... OR (visibility = 'protected'
--             AND EXISTS (SELECT 1 FROM tools.tool_grants g
--                          WHERE g.tool_id = tools.tool_id
--                            AND g.grantee_tenant_id = <current tenant>))
-- Until then `protected` is owner-only (identical read scope to `private`), platform stays
-- public, and NO grants table exists yet.
-- =====================================================================================
