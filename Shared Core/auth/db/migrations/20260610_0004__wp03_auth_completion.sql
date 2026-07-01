-- =====================================================================================
-- auth-service — WP03 (Auth completion I). PostgreSQL 16.
--
-- Adds the remaining first-cycle DB objects the WP03 package needs (Phase 2 Amendment
-- Log 2026-06 — Component 4 rate_limit_config, Component 1d /v1/usage rollup, the
-- tenants.plan enum guard, and the Component 5c behavior_policies shadow seed):
--
--   1. auth.rate_limit_config            self-protection limits (Component 4) + platform seed.
--   2. auth.tenant_usage_counters        /v1/usage rollup target (Component 1d / WP04) + RLS.
--   3. auth.tenants.plan                 enum CHECK guard (free | pro | enterprise).
--   4. auth.behavior_policies            ONE shadow seed row (Component 5c staging).
--
-- Idempotent: every object is guarded (IF NOT EXISTS / DO-block / ON CONFLICT), so the
-- file is safe to re-run. New constraints use a DO-block guard or CREATE UNIQUE INDEX —
-- never a bare ALTER ... ADD CONSTRAINT (which is not re-runnable).
-- =====================================================================================

-- ── 1. auth.rate_limit_config (Component 4 — self-protection rate limits) ─────────────
-- Per-endpoint Valkey fixed-window limits in front of Auth's own endpoints. `tenant_id`
-- NULL = platform default; non-NULL = an enterprise per-tenant override. The
-- `(endpoint, scope_kind, tenant_id)` uniqueness uses NULLS NOT DISTINCT (PG15+) so the
-- platform-default rows (tenant_id IS NULL) are themselves unique. PLATFORM-scoped: no RLS
-- (only Auth reads/writes it; the limiter loads every row in one platform-tx pass).
CREATE TABLE IF NOT EXISTS auth.rate_limit_config (
  config_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  endpoint         VARCHAR(100) NOT NULL,    -- '/v1/authorize' | '/v1/agents/{id}/token' |
                                             -- '/v1/service-tokens' | '/v1/admin/*' |
                                             -- '/v1/onboarding/signup'
  scope_kind       VARCHAR(30)  NOT NULL,    -- per-caller-service | per-tenant | per-agent |
                                             -- per-service | per-admin-agent | per-ip
  tenant_id        UUID,                     -- NULL = platform default; non-NULL = override
  limit_rpm        INTEGER      NOT NULL,
  burst_multiplier NUMERIC(4,2) NOT NULL DEFAULT 1.00,
  burst_seconds    INTEGER      NOT NULL DEFAULT 0,
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Unique (endpoint, scope_kind, tenant_id) with NULL tenants treated as a single value
-- (PG15+ NULLS NOT DISTINCT). A unique INDEX is re-runnable (IF NOT EXISTS); a bare
-- ADD CONSTRAINT is not.
CREATE UNIQUE INDEX IF NOT EXISTS ux_rate_limit_config_key
  ON auth.rate_limit_config (endpoint, scope_kind, tenant_id) NULLS NOT DISTINCT;

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.rate_limit_config TO auth_user;

-- Platform-default seed (idempotent). Mirrors Phase 2 Component 4 §"DDL + seed".
INSERT INTO auth.rate_limit_config (endpoint, scope_kind, limit_rpm, burst_multiplier, burst_seconds) VALUES
  ('/v1/authorize',         'per-caller-service', 5000, 2.00, 10),
  ('/v1/authorize',         'per-tenant',         2000, 2.00, 10),
  ('/v1/agents/{id}/token', 'per-agent',            60, 2.00, 30),
  ('/v1/agents/{id}/token', 'per-tenant',          600, 2.00, 30),
  ('/v1/service-tokens',    'per-service',          30, 1.00,  0),
  ('/v1/admin/*',           'per-admin-agent',      10, 1.00,  0),
  ('/v1/onboarding/signup', 'per-ip',               10, 1.00,  0)
ON CONFLICT (endpoint, scope_kind, tenant_id) DO NOTHING;

-- ── 2. auth.tenant_usage_counters (Component 1d / WP04 — /v1/usage rollup) ────────────
-- Hourly per-tenant usage buckets the (WP04) cypherx.llms.usage.recorded consumer rolls
-- into. /v1/usage reads ONLY this rollup (no cross-schema reads into llms.*). Tenant-scoped
-- (RLS on app.tenant_id), like the other tenant tables.
CREATE TABLE IF NOT EXISTS auth.tenant_usage_counters (
  tenant_id    UUID         NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  window_start TIMESTAMPTZ  NOT NULL,          -- hourly buckets (UTC, truncated)
  metric       VARCHAR(50)  NOT NULL,
               -- llm_requests | llm_tokens_in | llm_tokens_out | llm_cost_usd
  value        NUMERIC(20,6) NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, window_start, metric)
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'auth' AND c.relname = 'tenant_usage_counters' AND c.relrowsecurity
  ) THEN
    ALTER TABLE auth.tenant_usage_counters ENABLE ROW LEVEL SECURITY;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'auth' AND tablename = 'tenant_usage_counters'
      AND policyname = 'p_tenant_usage_counters_tenant'
  ) THEN
    CREATE POLICY p_tenant_usage_counters_tenant ON auth.tenant_usage_counters
      USING (tenant_id = current_setting('app.tenant_id')::uuid);
  END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.tenant_usage_counters TO auth_user;

-- ── 3. auth.tenants.plan enum guard (free | pro | enterprise) ─────────────────────────
-- The plan column already exists (20260606_0001) with default 'free'. WP03 adds the enum
-- CHECK so an invalid plan can never be persisted. DO-block-guarded so re-runs are safe
-- (a bare ALTER ... ADD CONSTRAINT would error on the second run).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'tenants_plan_chk' AND conrelid = 'auth.tenants'::regclass
  ) THEN
    ALTER TABLE auth.tenants
      ADD CONSTRAINT tenants_plan_chk CHECK (plan IN ('free','pro','enterprise'));
  END IF;
END
$$;

-- ── 4. auth.behavior_policies — ONE shadow seed row (Component 5c staging) ─────────────
-- Phase 2 first cycle = the table + ONE seeded platform-default policy row
-- (status='shadow', enforcement='alert') and NOTHING else (no middleware — that lands in
-- Phase 10/13). The row exists so later phases have a stable policy shape to evaluate
-- against. constraints JSON is Contract 17's shape. Fixed policy_id keeps the seed idempotent.
INSERT INTO auth.behavior_policies (policy_id, tenant_id, agent_id, name, version, status, constraints, enforcement, cooldown_seconds)
VALUES (
  '00000000-0000-0000-0000-0000000000b1',
  NULL,
  NULL,
  'default-behavior-shadow',
  1,
  'shadow',
  '{
    "rate_limits": {
      "tool_calls_per_minute":    50,
      "memory_reads_per_minute":  1000,
      "memory_writes_per_minute": 100,
      "llm_calls_per_minute":     30,
      "a2a_delegations_per_task": 10,
      "parallel_tasks":           5
    },
    "structural_limits": {
      "max_recursive_depth":         5,
      "max_subagent_spawn_per_task": 3,
      "max_tool_call_chain_length":  20
    },
    "sequence_rules": [],
    "anomaly_signals": {
      "token_burn_rate_per_hour_usd":    5.00,
      "tool_call_entropy_threshold":     0.85,
      "novel_tool_invocation_threshold": 3
    }
  }'::jsonb,
  'alert',
  300
)
ON CONFLICT (policy_id) DO NOTHING;

-- =====================================================================================
-- end 20260610_0004__wp03_auth_completion.sql
-- =====================================================================================
