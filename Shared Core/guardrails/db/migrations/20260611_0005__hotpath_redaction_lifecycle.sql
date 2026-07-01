-- =====================================================================================
-- guardrails-service — WP07 hot-path hardening + redaction-key lifecycle. PostgreSQL 16.
--
-- Migration number 0004 (follows 0001 init, 0002 seed, 0003 policy_authoring). Top-to-
-- bottom runnable; fully idempotent.
--
-- Adds:
--   1. guardrails.rules.cost_usd      — per-rule-evaluation price for Contract-19.1 usage
--      metering (the check's cost_usd = sum of evaluated rules' cost; a configured flat
--      fallback applies until per-rule prices are populated). Authored here so pricing is
--      DB-authoritative + auditable, not hardcoded.
--   2. guardrails.tenant_redaction_keys lifecycle columns + a PLUGGABLE key_ref scheme:
--        * widen the key_ref CHECK to accept 'env:' / 'sealed:' (and keep legacy
--          'secretsmanager:') so first-cycle BYO/dev keys resolve without AWS.
--        * add ``version`` (monotonic per tenant) for auditability of rotations.
--        * add the 'pending' status already in the CHECK (no change) — used by a future
--          two-phase activation; first cycle uses current -> retired demotion.
--      Plus a covering index for the resolver's "current, else newest in-grace retired".
--   3. DELETE grant on tenant_redaction_keys for the grace-window retirement job.
-- =====================================================================================

-- ── 1. Per-rule cost (Contract 19.1 metering) ─────────────────────────────────────────
ALTER TABLE guardrails.rules
  ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12, 8) NOT NULL DEFAULT 0;

-- ── 2. Redaction-key lifecycle: pluggable key_ref scheme + rotation version ───────────
-- Widen the key_ref scheme. The original CHECK only admitted 'secretsmanager:%'; WP07 adds
-- the 'env:' (dev/BYO) and 'sealed:' (prod sealed-secret) schemes. Drop + re-add so the
-- new constraint takes effect; named so it is idempotent across re-runs.
ALTER TABLE guardrails.tenant_redaction_keys
  DROP CONSTRAINT IF EXISTS tenant_redaction_keys_key_ref_check;
ALTER TABLE guardrails.tenant_redaction_keys
  ADD CONSTRAINT tenant_redaction_keys_key_ref_check
  CHECK (
    key_ref LIKE 'env:%'
    OR key_ref LIKE 'sealed:%'
    OR key_ref LIKE 'secretsmanager:%'  -- legacy alias (resolves via the prod sealed path)
  );

-- Monotonic rotation version per tenant (audit/debug; the resolver keys off status/created_at).
ALTER TABLE guardrails.tenant_redaction_keys
  ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- Resolver covering index: current key first, then newest still-in-grace retired key.
CREATE INDEX IF NOT EXISTS ix_tenant_redaction_resolve
  ON guardrails.tenant_redaction_keys (tenant_id, status, created_at DESC);
-- Retirement-job scan: retired rows ordered by grace clock.
CREATE INDEX IF NOT EXISTS ix_tenant_redaction_retired
  ON guardrails.tenant_redaction_keys (retired_at)
  WHERE status = 'retired';

-- ── 3. Grants: the retirement job DELETEs grace-expired retired keys ──────────────────
-- (0001 granted SELECT/INSERT/UPDATE; add DELETE for the lifespan-scheduled sweep. RLS
-- still applies per-row, so the sweep runs tenant-by-tenant under each tenant's scope.)
GRANT DELETE ON guardrails.tenant_redaction_keys TO grd_user;
