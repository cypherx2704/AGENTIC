-- =====================================================================================
-- llms-gateway — WP05 rate limits (Phase 3, Amendment Log 2026-06). PostgreSQL 16.
-- Idempotent: safe to re-run top-to-bottom.
--
-- Adds `llms.rate_limits` — per-plan-tier rate/quota reference config consumed by the
-- WP05 rate-limiter (`services/rate_limit.py`) and the plan/limits resolver
-- (`services/auth_client.py`).
--
-- PLATFORM-SCOPED reference config (no tenant_id, no RLS — mirrors provider_pricing /
-- model_capabilities): the same three rows (free|pro|enterprise) apply to every tenant.
-- A request's plan tier comes from the JWT `plan` claim (Auth WP03 stamps it) or, as a
-- fallback, the Auth `/v1/tenants/{id}/limits` endpoint; this table is the source the
-- gateway falls back to for the EFFECTIVE limit numbers and the seed-of-record that
-- mirrors the Auth `plan_defaults` `llms` block (Contract-19).
--
-- Column-per-limit (not JSONB) chosen for: typed/constrained columns, cheap indexable
-- reads, and 1:1 correspondence with the Contract-19 `llms` block keys so the row maps
-- straight onto the resolver's `PlanLimits` dataclass. Updates are PR-managed
-- (read-only at runtime), exactly like provider_pricing.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS llms;  -- standalone-safe (created in 0001)

-- ── rate_limits (platform-scoped — no tenant_id, no RLS) ──────────────────────────────
-- One row per plan tier. Limit columns match the Contract-19 `llms` block keys 1:1.
--   requests_per_min            : fixed-window request cap per tenant per minute
--   prompt_tokens_per_min       : prompt (input) tokens per tenant per minute
--   completion_tokens_per_min   : completion (output) tokens per tenant per minute
--   cost_usd_per_hour/day/month : rolling spend caps (enforced by budget logic, WP-later;
--                                 carried here so the row is the complete Contract-19 block)
CREATE TABLE IF NOT EXISTS llms.rate_limits (
  plan                      VARCHAR(20)   PRIMARY KEY,            -- free | pro | enterprise
  requests_per_min          INTEGER       NOT NULL,
  prompt_tokens_per_min     BIGINT        NOT NULL,
  completion_tokens_per_min BIGINT        NOT NULL,
  cost_usd_per_hour         NUMERIC(12,4) NOT NULL DEFAULT 0,
  cost_usd_per_day          NUMERIC(12,4) NOT NULL DEFAULT 0,
  cost_usd_per_month        NUMERIC(12,4) NOT NULL DEFAULT 0,
  updated_at                TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_rate_limits_plan CHECK (plan IN ('free', 'pro', 'enterprise'))
);

-- Seed the three tiers, mirroring the Auth `plan_defaults` `llms` block. Cost caps are
-- left at 0 here (budget enforcement is a later WP); the token/request caps are
-- authoritative. ON CONFLICT keeps the row current on re-run (PR-managed updates).
INSERT INTO llms.rate_limits
  (plan, requests_per_min, prompt_tokens_per_min, completion_tokens_per_min,
   cost_usd_per_hour, cost_usd_per_day, cost_usd_per_month)
VALUES
  ('free',           60,    100000,    50000, 0, 0, 0),
  ('pro',           600,   2000000,  1000000, 0, 0, 0),
  ('enterprise',  10000, 100000000, 50000000, 0, 0, 0)
ON CONFLICT (plan) DO UPDATE SET
  requests_per_min          = EXCLUDED.requests_per_min,
  prompt_tokens_per_min     = EXCLUDED.prompt_tokens_per_min,
  completion_tokens_per_min = EXCLUDED.completion_tokens_per_min,
  cost_usd_per_hour         = EXCLUDED.cost_usd_per_hour,
  cost_usd_per_day          = EXCLUDED.cost_usd_per_day,
  cost_usd_per_month        = EXCLUDED.cost_usd_per_month,
  updated_at                = NOW();

-- ── GRANTS (platform-scoped, read-only at runtime — PR-managed updates) ───────────────
GRANT SELECT ON llms.rate_limits TO llms_user;
