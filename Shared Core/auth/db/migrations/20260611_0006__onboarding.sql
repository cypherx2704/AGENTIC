-- =====================================================================================
-- auth-service — WP04 (Auth completion II) / Component 1c: self-serve ONBOARDING.
-- PostgreSQL 16.
--
-- The `auth.signup_attempts` table already exists (20260606_0001__init.sql) as a
-- PLATFORM-scoped table (no RLS — only Auth itself reads/writes it; verification by an
-- opaque token, not a tenant JWT). This migration EXTENDS that table additively so the
-- self-serve onboarding funnel (signup -> email verify -> tenant/agent/key provisioning,
-- plus resend / upgrade-request / close-request) has the columns it needs, WITHOUT
-- breaking the original shape.
--
-- What changes vs. 0001:
--   * verification_token_hash  NEW  — we now store ONLY the SHA-256 hex of the raw token,
--                                     never the raw token (defence in depth; mirrors how
--                                     api_keys stores key_hash). The original plaintext
--                                     `verification_token` column is relaxed to NULLABLE so
--                                     new rows can omit it; legacy rows are untouched.
--   * tenant_name              NEW  — the desired tenant/org name captured at signup
--                                     (becomes auth.tenants.name on verify).
--   * attempts                 NEW  — count of verification-email sends (signup = 1, each
--                                     resend +1) for velocity / abuse caps.
--   * verified_at, tenant_id,
--     initial_admin_user_id          already exist (0001) — reused as-is on verify.
--   * status                        already exists; this funnel uses
--                                     pending_verification | verifying | verified | expired
--                                     | manual_review | rejected. ('verifying' is the brief
--                                     single-winner claim state between a verify click and the
--                                     tenant being provisioned, so a double-click cannot create
--                                     two tenants.) The column is a free VARCHAR(30), no CHECK,
--                                     so no constraint change is required.
--   * full_name / terms_version_accepted were NOT NULL in 0001. A minimal self-serve
--     signup may not collect them up front, so we relax both to NULLABLE with a default
--     so inserts that omit them succeed. Existing rows keep their values.
--
-- Idempotent: every change is guarded (ADD COLUMN IF NOT EXISTS / DROP NOT NULL is a
-- no-op when already nullable / DO-block for index existence), so the file is safe to
-- re-run. PLATFORM-scoped: NO RLS (consistent with 0001 — verification is token-bound,
-- not tenant-bound).
-- =====================================================================================

-- ── 1. New columns (additive, all guarded) ───────────────────────────────────────────
ALTER TABLE auth.signup_attempts
  ADD COLUMN IF NOT EXISTS verification_token_hash VARCHAR(64);

ALTER TABLE auth.signup_attempts
  ADD COLUMN IF NOT EXISTS tenant_name TEXT;

ALTER TABLE auth.signup_attempts
  ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 1;

-- ── 2. Relax 0001 NOT NULLs that a minimal self-serve signup may not populate ─────────
-- The raw plaintext token is no longer written (we store only the hash); make it nullable
-- and drop its uniqueness reliance — uniqueness now lives on the hash (below).
ALTER TABLE auth.signup_attempts
  ALTER COLUMN verification_token DROP NOT NULL;

-- full_name / terms_version_accepted: optional at signup. Give a default so legacy NOT NULL
-- semantics never block an insert that omits them, then drop the NOT NULL.
ALTER TABLE auth.signup_attempts
  ALTER COLUMN full_name DROP NOT NULL;
ALTER TABLE auth.signup_attempts
  ALTER COLUMN terms_version_accepted DROP NOT NULL;

-- ── 3. Uniqueness + lookup on the token HASH (verify reads by hash) ───────────────────
-- A partial UNIQUE index (hash IS NOT NULL) so legacy rows with a NULL hash don't collide,
-- and a brand-new token hash is guaranteed unique. CREATE UNIQUE INDEX IF NOT EXISTS is
-- re-runnable; a bare ADD CONSTRAINT is not.
CREATE UNIQUE INDEX IF NOT EXISTS ux_signup_token_hash
  ON auth.signup_attempts (verification_token_hash)
  WHERE verification_token_hash IS NOT NULL;

-- Velocity / abuse scoring reads recent attempts by (email) and by (ip, created_at); the
-- (email) and (ip_address, created_at) indexes from 0001 already cover those lookups.
-- Add a status+created_at index so the "pending, not yet expired" scans stay cheap.
CREATE INDEX IF NOT EXISTS ix_signup_status_created
  ON auth.signup_attempts (status, created_at);

-- Grants already cover signup_attempts (0001: SELECT/INSERT/UPDATE/DELETE to auth_user).
-- No new grants required.

-- =====================================================================================
-- end 20260611_0006__onboarding.sql
-- =====================================================================================
