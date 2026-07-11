-- =====================================================================================
-- guardrails-service — B7: per-request canary-token leak detector (output rule).
-- PostgreSQL 16.
--
-- WHY: the existing output-jailbreak-leak-v1 matches generic phrases ("my system prompt")
-- and both false-positives on benign self-reference and misses paraphrased leaks. A
-- per-request random canary the caller embeds in its own system prompt is a near-zero
-- false-positive, phrasing-independent signal that the system prompt/context leaked. The
-- code adds `output-canary-leak-v1` (direction=output, block) whose detector is INERT unless
-- the caller supplies `canary_tokens` AND `CANARY_LEAK_ENABLED` is set — so the default path
-- is byte-identical. The registry-consistency gate still requires a DB platform row (else
-- /readyz -> 503) and the platform-default policy must enable it, so this migration adds both.
--
-- Idempotent: ON CONFLICT DO NOTHING + append-only policy update. Safe to re-run.
-- =====================================================================================

-- ── 1) registry row (platform; tenant_id IS NULL) ────────────────────────────────────
INSERT INTO guardrails.rules
  (rule_id, tenant_id, version, default_action, default_fail_mode, default_stream_mode,
   default_severity, default_category, direction, timeout_ms, status)
VALUES
  ('output-canary-leak-v1', NULL, '1', 'block', 'closed', 'buffer', 'high', 'security', 'output', 10, 'active')
ON CONFLICT (rule_id) WHERE tenant_id IS NULL DO NOTHING;

-- ── 2) enable it in the platform-default policy (append only if missing) ─────────────
UPDATE guardrails.policies p
SET rules = p.rules || (
  SELECT COALESCE(jsonb_agg(e), '[]'::jsonb)
    FROM jsonb_array_elements(
      '[{"rule_id":"output-canary-leak-v1","enabled":true,"action_override":null}]'::jsonb
    ) AS e
   WHERE NOT EXISTS (
     SELECT 1 FROM jsonb_array_elements(p.rules) x WHERE x->>'rule_id' = e->>'rule_id'
   )
)
WHERE p.policy_id = '00000000-0000-0000-0000-0000000d0001';

-- =====================================================================================
-- end 20260710_0008__canary_leak.sql
-- =====================================================================================
