-- =====================================================================================
-- auth-service — WP04 (Auth completion II). PostgreSQL 16.
--
-- Outbound WEBHOOKS + signed-delivery worker (Contract 21):
--
--   1. auth.webhook_subscriptions  per-tenant endpoint registrations (url + event filter +
--                                  envelope-encrypted signing secret) + RLS + grants.
--   2. auth.webhook_deliveries     the per-event delivery queue the WebhookDeliveryWorker drains
--                                  (POSTs the payload with an HMAC-SHA256 signature header,
--                                  exponential-backoff retry, dead after N attempts) + RLS + grants.
--
-- Both tables are TENANT-SCOPED (tenant_id + tenant-leading index + RLS USING app.tenant_id),
-- the same shape as auth.agents / auth.api_keys (Contract 13). Access goes through
-- TenantTx.inTenant(tenantId){...}; the worker enumerates due deliveries per tenant.
--
-- Idempotent: every object is guarded (IF NOT EXISTS / DO-block), so the file is safe to re-run.
-- New RLS/policies use a DO-block guard — never a bare ENABLE/CREATE POLICY (not re-runnable).
-- =====================================================================================

-- ── 1. auth.webhook_subscriptions ────────────────────────────────────────────────────
-- One row per registered endpoint. `event_types` is a TEXT[] of fully-qualified event-type
-- filters (e.g. 'cypherx.tenant.created', or '*' for all). `secret_enc` holds the per-sub
-- HMAC signing secret, envelope-encrypted with the same KeyEncryptor used for signing keys
-- (raw secret is returned to the caller exactly ONCE at create / rotate-secret, never stored
-- in clear). `status` gates delivery: paused subs enqueue nothing and the worker skips them.
CREATE TABLE IF NOT EXISTS auth.webhook_subscriptions (
  sub_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID         NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  url          TEXT         NOT NULL,
  event_types  TEXT[]       NOT NULL DEFAULT '{}',   -- fully-qualified types; '*' = all
  secret_enc   BYTEA        NOT NULL,                 -- KeyEncryptor envelope of the HMAC secret
  status       VARCHAR(20)  NOT NULL DEFAULT 'active',-- active | paused
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT webhook_subscriptions_status_chk CHECK (status IN ('active', 'paused'))
);
CREATE INDEX IF NOT EXISTS idx_webhook_subs_tenant ON auth.webhook_subscriptions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_webhook_subs_active
  ON auth.webhook_subscriptions(tenant_id) WHERE status = 'active';

-- ── 2. auth.webhook_deliveries ───────────────────────────────────────────────────────
-- The delivery queue. One row per (subscription, event) pair to deliver. The worker polls
-- rows whose status is 'pending' OR ('failed' with next_attempt_at <= now), POSTs payload to
-- the sub's url, and updates status/attempts/next_attempt_at/last_status_code. Terminal
-- states: 'delivered' (2xx) and 'dead' (attempts exhausted). `payload` is the Contract 5
-- envelope (or any JSON body) we sign and send verbatim.
CREATE TABLE IF NOT EXISTS auth.webhook_deliveries (
  delivery_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  sub_id           UUID         NOT NULL REFERENCES auth.webhook_subscriptions(sub_id) ON DELETE CASCADE,
  tenant_id        UUID         NOT NULL,
  event_type       VARCHAR(200) NOT NULL,
  payload          JSONB        NOT NULL,
  status           VARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending | delivered | failed | dead
  attempts         INTEGER      NOT NULL DEFAULT 0,
  next_attempt_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  last_status_code INTEGER,
  last_error       TEXT,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  delivered_at     TIMESTAMPTZ,
  CONSTRAINT webhook_deliveries_status_chk CHECK (status IN ('pending', 'delivered', 'failed', 'dead'))
);
-- Worker scan index: due rows oldest-first, only the non-terminal states it polls.
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
  ON auth.webhook_deliveries(next_attempt_at)
  WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_tenant_sub
  ON auth.webhook_deliveries(tenant_id, sub_id, created_at DESC);

-- ── Row Level Security (Contract 13) ──────────────────────────────────────────────────
-- DO-block guards keep ENABLE / CREATE POLICY idempotent (a bare statement errors on re-run).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'auth' AND c.relname = 'webhook_subscriptions' AND c.relrowsecurity
  ) THEN
    ALTER TABLE auth.webhook_subscriptions ENABLE ROW LEVEL SECURITY;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'auth' AND c.relname = 'webhook_deliveries' AND c.relrowsecurity
  ) THEN
    ALTER TABLE auth.webhook_deliveries ENABLE ROW LEVEL SECURITY;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'auth' AND tablename = 'webhook_subscriptions'
      AND policyname = 'p_webhook_subscriptions_tenant'
  ) THEN
    CREATE POLICY p_webhook_subscriptions_tenant ON auth.webhook_subscriptions
      USING (tenant_id = current_setting('app.tenant_id')::uuid);
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'auth' AND tablename = 'webhook_deliveries'
      AND policyname = 'p_webhook_deliveries_tenant'
  ) THEN
    CREATE POLICY p_webhook_deliveries_tenant ON auth.webhook_deliveries
      USING (tenant_id = current_setting('app.tenant_id')::uuid);
  END IF;
END
$$;

-- ── Grants to the runtime role (RLS still applies on top) ─────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.webhook_subscriptions TO auth_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.webhook_deliveries     TO auth_user;

-- =====================================================================================
-- end 20260611_0007__webhooks.sql
-- =====================================================================================
