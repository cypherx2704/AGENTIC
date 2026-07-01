-- =====================================================================================
-- auth-service — first-cycle seed data (Phase 2). PostgreSQL 16.
-- Idempotent: every INSERT uses ON CONFLICT DO NOTHING. Safe to re-run.
--
-- SYSTEM-USER sentinel:
--   created_by on auth.agents is NOT NULL. Bootstrap / manual-seed agents that have no
--   px0 user behind them use the well-known SYSTEM-USER sentinel UUID:
--       00000000-0000-0000-0000-000000000000
--   This is NOT a row in any users table (px0 owns users) — it is a reserved constant the
--   application code references (see ai.cypherx.auth.domain SYSTEM_USER_ID). It is documented
--   here so DB readers understand provenance of seed/bootstrap agent rows.
-- =====================================================================================

-- ── Well-known tenants (Contract 13) ──────────────────────────────────────────────────
--   ...-0001 = platform tenant (Auth's own admin agents live here)
--   ...-00ff = integration-test tenant (rejected in prod via ENVIRONMENT gate)
INSERT INTO auth.tenants (tenant_id, name, plan, source) VALUES
  ('00000000-0000-0000-0000-000000000001', 'platform',         'enterprise', 'manual-seed'),
  ('00000000-0000-0000-0000-0000000000ff', 'integration-test', 'free',       'manual-seed')
ON CONFLICT (tenant_id) DO NOTHING;

-- ── plan_defaults (Contract 19.2 — limits shape from usage/tenant-quotas.schema.json) ─
-- Keys are per-service blocks; absence of a key means "unlimited / inherit". Values below
-- are sensible first-cycle defaults: modest free tier, mid pro tier, high-cap enterprise.
INSERT INTO auth.plan_defaults (plan, limits) VALUES
  ('free', '{
     "auth":       { "agents_max": 5,   "api_keys_per_agent_max": 2,  "tokens_issued_per_min": 60 },
     "llms":       { "requests_per_min": 60,   "prompt_tokens_per_min": 100000,  "completion_tokens_per_min": 50000,
                     "cost_usd_per_hour": 1.0, "cost_usd_per_day": 5.0, "cost_usd_per_month": 25.0, "byok_keys_max": 1 },
     "guardrails": { "checks_per_min": 120, "input_bytes_per_min": 1048576, "custom_rules_max": 5,  "custom_policies_max": 2 },
     "rag":        { "kbs_max": 2,  "documents_per_kb_max": 1000,  "storage_bytes_max": 524288000,  "queries_per_min": 60,  "ingest_jobs_per_hour": 5 },
     "memory":     { "memories_max": 10000,  "storage_bytes_max": 524288000,  "stores_per_min": 120,  "retrieves_per_min": 600 },
     "tools":      { "private_tools_max": 5,  "invocations_per_min": 60,  "publishable_versions_max": 2 },
     "skills":     { "private_skills_max": 5,  "executions_per_min": 60 },
     "xagent":     { "agents_max": 5,  "concurrent_tasks_max": 3,  "workflow_depth_max": 5 }
   }'::jsonb),
  ('pro', '{
     "auth":       { "agents_max": 50,  "api_keys_per_agent_max": 10, "tokens_issued_per_min": 600 },
     "llms":       { "requests_per_min": 600,  "prompt_tokens_per_min": 2000000, "completion_tokens_per_min": 1000000,
                     "cost_usd_per_hour": 50.0, "cost_usd_per_day": 500.0, "cost_usd_per_month": 5000.0, "byok_keys_max": 10 },
     "guardrails": { "checks_per_min": 1200, "input_bytes_per_min": 52428800, "custom_rules_max": 100, "custom_policies_max": 25 },
     "rag":        { "kbs_max": 50, "documents_per_kb_max": 100000, "storage_bytes_max": 53687091200, "queries_per_min": 600, "ingest_jobs_per_hour": 100 },
     "memory":     { "memories_max": 1000000, "storage_bytes_max": 53687091200, "stores_per_min": 1200, "retrieves_per_min": 6000 },
     "tools":      { "private_tools_max": 100, "invocations_per_min": 600, "publishable_versions_max": 50 },
     "skills":     { "private_skills_max": 100, "executions_per_min": 600 },
     "xagent":     { "agents_max": 50, "concurrent_tasks_max": 25, "workflow_depth_max": 10 }
   }'::jsonb),
  ('enterprise', '{
     "auth":       { "agents_max": 10000, "api_keys_per_agent_max": 100, "tokens_issued_per_min": 10000 },
     "llms":       { "requests_per_min": 10000, "prompt_tokens_per_min": 100000000, "completion_tokens_per_min": 50000000,
                     "cost_usd_per_hour": 5000.0, "cost_usd_per_day": 50000.0, "cost_usd_per_month": 1000000.0, "byok_keys_max": 100 },
     "guardrails": { "checks_per_min": 50000, "input_bytes_per_min": 1073741824, "custom_rules_max": 5000, "custom_policies_max": 1000 },
     "rag":        { "kbs_max": 5000, "documents_per_kb_max": 10000000, "storage_bytes_max": 5497558138880, "queries_per_min": 50000, "ingest_jobs_per_hour": 5000 },
     "memory":     { "memories_max": 1000000000, "storage_bytes_max": 5497558138880, "stores_per_min": 50000, "retrieves_per_min": 200000 },
     "tools":      { "private_tools_max": 10000, "invocations_per_min": 50000, "publishable_versions_max": 5000 },
     "skills":     { "private_skills_max": 10000, "executions_per_min": 50000 },
     "xagent":     { "agents_max": 10000, "concurrent_tasks_max": 1000, "workflow_depth_max": 25 }
   }'::jsonb)
ON CONFLICT (plan) DO NOTHING;

-- ── Default platform-default RBAC policy (Component 5 — tenant_id IS NULL) ─────────────
-- One row, name = 'default-allow-first-cycle'. Allows the six first-cycle action scopes.
-- The decision engine evaluates WHERE tenant_id = $1 OR tenant_id IS NULL
-- ORDER BY tenant_id NULLS LAST, so a future per-tenant override wins over this default.
INSERT INTO auth.policies (policy_id, tenant_id, name, description, version, status, rules)
VALUES (
  '00000000-0000-0000-0000-0000000000a1',
  NULL,
  'default-allow-first-cycle',
  'Platform default: allow the six first-cycle action scopes for all tenants.',
  1,
  'active',
  '[
    { "action": "llm:invoke",       "effect": "allow", "conditions": [] },
    { "action": "memory:read",      "effect": "allow", "conditions": [] },
    { "action": "memory:write",     "effect": "allow", "conditions": [] },
    { "action": "rag:query",        "effect": "allow", "conditions": [] },
    { "action": "tool:invoke",      "effect": "allow", "conditions": [] },
    { "action": "guardrails:check", "effect": "allow", "conditions": [] }
  ]'::jsonb
)
ON CONFLICT (policy_id) DO NOTHING;

-- ── service_acl — FIRST-CYCLE 5 edges (Component 8b) ──────────────────────────────────
-- Service names align with ECR repo names (Phase 1 Component 5).
-- allowed_scopes are the internal scopes the caller may mint when targeting that service.
INSERT INTO auth.service_acl (caller_service, target_service, allowed_scopes) VALUES
  ('xagent',             'auth-service',       ARRAY['internal:read']),
  ('xagent',             'llms-gateway',       ARRAY['internal:read','internal:write']),
  ('xagent',             'guardrails-service', ARRAY['internal:read','internal:write']),
  ('llms-gateway',       'auth-service',       ARRAY['internal:read']),
  ('guardrails-service', 'auth-service',       ARRAY['internal:read'])
ON CONFLICT (caller_service, target_service) DO NOTHING;

-- HOW TO ADD MORE EDGES LATER (memory / rag / tools come online in later phases):
--   Add rows mirroring the pattern above, e.g. when memory-service / rag-service / tools-service
--   ship, insert the caller→target edges they need. Examples (DO NOT enable until those
--   services exist):
--     ('xagent',        'memory-service', ARRAY['internal:read','internal:write']),
--     ('xagent',        'rag-service',    ARRAY['internal:read']),
--     ('xagent',        'tools-service',  ARRAY['internal:read','internal:write']),
--     ('memory-service','auth-service',   ARRAY['internal:read']),
--     ('rag-service',   'auth-service',   ARRAY['internal:read']),
--     ('tools-service', 'auth-service',   ARRAY['internal:read'])
--   Keep ON CONFLICT (caller_service, target_service) DO NOTHING so re-runs are idempotent.

-- =====================================================================================
-- end 20260606_0002__seed.sql
-- =====================================================================================
