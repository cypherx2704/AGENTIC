-- =====================================================================================
-- auth-service — end-user login sessions (refresh tokens for the Console/BFF).
-- PostgreSQL 16. Idempotent (re-runnable). Apply AFTER 20260623_0012 (hil_framework).
--
-- Why: an end-user login (UserAuthService) mints an ORCHESTRATOR agent JWT that Contract 1 caps at
-- <= 1h. With no renewal path the Console session hard-expires mid-work (unsaved Tool-Builder flows
-- are lost). This table backs a refresh token the BFF holds server-side and uses to silently re-mint
-- the <=1h access token while the user is active — access token stays <=1h (Contract 1 intact),
-- effective session lasts up to an absolute cap with an idle timeout.
--
-- Model (matches how the auth-service already treats logged-in humans):
--   * PLATFORM-SCOPED (NO RLS) — like auth.users/signup_attempts: a refresh, like a login, resolves
--     the user/session BEFORE any tenant context (app.tenant_id) exists, so it is read/written via
--     TenantTx.inPlatform(). It carries tenant_id as a plain FK (no RLS policy).
--   * The raw refresh secret is NEVER stored — only its SHA-256 (refresh_token_hash). The token the
--     BFF holds is "<session_id>.<secret>"; the server looks the row up by session_id (PK) and
--     constant-time compares the hash.
--   * Non-rotating by design: a refresh slides last_used_at (idle window) but does not change the
--     hash — so concurrent BFF proxy requests cannot rotation-race each other into a logout. The hard
--     absolute cap (absolute_expires_at) and the idle window (last_used_at + idle_timeout_seconds)
--     bound the session; suspending the user also kills it (refresh re-checks users.status).
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =====================================================================================
-- PLATFORM-SCOPED: auth.user_sessions (no RLS — mirrors auth.users)
-- =====================================================================================
CREATE TABLE IF NOT EXISTS auth.user_sessions (
  session_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              UUID NOT NULL REFERENCES auth.users(user_id)   ON DELETE CASCADE,
  tenant_id            UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  refresh_token_hash   TEXT        NOT NULL,               -- SHA-256 hex of the opaque secret; never the raw token
  issued_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- original login — anchors the absolute cap
  last_used_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- slides on each refresh — anchors the idle timeout
  absolute_expires_at  TIMESTAMPTZ NOT NULL,                -- issued_at + absolute TTL (hard cap, never extended)
  idle_timeout_seconds INTEGER     NOT NULL,                -- idle expiry = last_used_at + this many seconds
  revoked_at           TIMESTAMPTZ,                         -- NULL = active; set on logout / suspend / reuse
  revoked_reason       TEXT,
  user_agent           TEXT,                                -- best-effort, for session management/audit
  ip_address           TEXT,                                -- best-effort, for session management/audit
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT user_sessions_idle_positive_chk CHECK (idle_timeout_seconds > 0)
);

-- A refresh secret hash is globally unique (integrity guard against collision/reuse across rows).
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_sessions_refresh_hash
  ON auth.user_sessions (refresh_token_hash);
-- Revoke-all-for-user (logout everywhere / on password change) + list a user's sessions.
CREATE INDEX IF NOT EXISTS idx_user_sessions_user   ON auth.user_sessions (user_id);
-- Housekeeping sweep of dead rows (past absolute cap).
CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry ON auth.user_sessions (absolute_expires_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.user_sessions TO auth_user;

-- =====================================================================================
-- end 20260712_0013__user_sessions.sql
-- =====================================================================================
