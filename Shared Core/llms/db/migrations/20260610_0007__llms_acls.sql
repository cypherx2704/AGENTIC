-- =====================================================================================
-- llms-gateway — WP06 per-key ACLs (Contract-18). PostgreSQL 16.
-- Idempotent: safe to re-run top-to-bottom.
--
-- Adds `llms.api_key_acls` — OPTIONAL per-API-key allow-lists that constrain which
-- models / providers / operations a given Auth-minted API key may invoke (Contract-18).
--
-- TENANT-SCOPED (tenant_id + tenant-leading index + RLS USING app.tenant_id), exactly
-- like usage_records: every read runs inside the Core `in_tenant()` helper so RLS admits
-- ONLY the caller's tenant rows. The key the ACL is keyed by is the Auth `api_key_id`
-- claim from the JWT (Principal.api_key_id) — never a body/param.
--
-- SEMANTICS (enforced in services/acl.py):
--   * A key with NO acl rows is UNRESTRICTED — this is the Contract-18 default. The ACL
--     is opt-in: tenants insert rows only for keys they want to scope down.
--   * Each of the three array columns is per-dimension: NULL = "no restriction on that
--     dimension"; a non-NULL array must CONTAIN the requested value to permit it.
--   * When a key has >=1 row, the request is allowed iff at least one row permits the
--     model AND provider AND operation (a row "permits" a dimension when its array is
--     NULL or contains the value). Otherwise -> 403 FORBIDDEN (reason ACL_DENIED).
--
-- allowed_operations values: 'chat' | 'embedding' (mirrors usage_records.operation).
-- Updates are tenant-managed (admin surface, a later WP); read-only on the hot path.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() (standalone-safe; created in 0001)

CREATE SCHEMA IF NOT EXISTS llms;  -- standalone-safe (created in 0001)

-- ── api_key_acls (tenant-scoped — tenant_id + RLS) ────────────────────────────────────
-- One key MAY have multiple rows (e.g. one row allowing chat on a model set, another
-- allowing embedding on a different set). NULL array = unrestricted on that dimension.
CREATE TABLE IF NOT EXISTS llms.api_key_acls (
  acl_id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID         NOT NULL,
  api_key_id          UUID         NOT NULL,        -- the Auth api_key_id from the JWT
  allowed_models      TEXT[],                       -- NULL = any model
  allowed_providers   TEXT[],                       -- NULL = any provider
  allowed_operations  TEXT[],                       -- NULL = any operation ('chat'|'embedding')
  created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Tenant-leading lookup index for the hot-path load of a key's ACL rows.
CREATE INDEX IF NOT EXISTS idx_api_key_acls_tenant_key
  ON llms.api_key_acls (tenant_id, api_key_id);

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — tenant-scoped, identical pattern to usage_records.
-- Every read runs inside a tx that does SELECT set_config('app.tenant_id','<uuid>',true).
-- =====================================================================================

ALTER TABLE llms.api_key_acls ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_api_key_acls_tenant ON llms.api_key_acls;
CREATE POLICY p_api_key_acls_tenant ON llms.api_key_acls FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- =====================================================================================
-- GRANTS to the runtime role (llms_user). RLS still applies on top of these.
-- App reads on the hot path; tenant-scoped writes via a later admin surface.
-- =====================================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON llms.api_key_acls TO llms_user;
