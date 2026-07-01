-- =====================================================================================
-- guardrails-service — additive PII coverage: SSN/IP/address detectors + output PII for
-- phone/SSN/IP/address (closes the /v1/check/output PII leak). PostgreSQL 16.
--
-- WHY: the first-cycle rule set had NO ssn/ip/address detectors and the OUTPUT direction
-- only redacted email + credit-card, so phone/SSN/IP/address leaked through on
-- /v1/check/output (a real eval found phone 555-867-5309 surviving). The code adds the
-- detectors as built-in RuleSpecs; the registry-consistency gate (readyz) requires the DB
-- `guardrails.rules` platform rows to mirror the built-ins, and the platform-default policy
-- must ENABLE them for them to run. This migration adds both.
--
-- Idempotent: rules INSERT uses ON CONFLICT DO NOTHING; the policy UPDATE appends only the
-- rule_ids not already present. Safe to re-run.
-- =====================================================================================

-- ── 1) registry rows (platform; tenant_id IS NULL) ───────────────────────────────────
INSERT INTO guardrails.rules
  (rule_id, tenant_id, version, default_action, default_fail_mode, default_stream_mode,
   default_severity, default_category, direction, timeout_ms, status)
VALUES
  ('pii-ssn-v1',             NULL, '1', 'redact', 'closed', 'buffer', 'high',   'pii', 'input',  10, 'active'),
  ('pii-ip-v1',              NULL, '1', 'redact', 'closed', 'buffer', 'low',    'pii', 'input',  10, 'active'),
  ('pii-address-v1',         NULL, '1', 'redact', 'closed', 'buffer', 'medium', 'pii', 'input',  10, 'active'),
  ('output-pii-phone-v1',    NULL, '1', 'redact', 'closed', 'buffer', 'medium', 'pii', 'output', 10, 'active'),
  ('output-pii-ssn-v1',      NULL, '1', 'redact', 'closed', 'buffer', 'high',   'pii', 'output', 10, 'active'),
  ('output-pii-ip-v1',       NULL, '1', 'redact', 'closed', 'buffer', 'low',    'pii', 'output', 10, 'active'),
  ('output-pii-address-v1',  NULL, '1', 'redact', 'closed', 'buffer', 'medium', 'pii', 'output', 10, 'active')
ON CONFLICT (rule_id) WHERE tenant_id IS NULL DO NOTHING;

-- ── 2) enable them in the platform-default policy (append only the missing rule_ids) ──
UPDATE guardrails.policies p
SET rules = p.rules || (
  SELECT COALESCE(jsonb_agg(e), '[]'::jsonb)
    FROM jsonb_array_elements(
      '[{"rule_id":"pii-ssn-v1","enabled":true,"action_override":null},
        {"rule_id":"pii-ip-v1","enabled":true,"action_override":null},
        {"rule_id":"pii-address-v1","enabled":true,"action_override":null},
        {"rule_id":"output-pii-phone-v1","enabled":true,"action_override":null},
        {"rule_id":"output-pii-ssn-v1","enabled":true,"action_override":null},
        {"rule_id":"output-pii-ip-v1","enabled":true,"action_override":null},
        {"rule_id":"output-pii-address-v1","enabled":true,"action_override":null}]'::jsonb
    ) AS e
   WHERE NOT EXISTS (
     SELECT 1 FROM jsonb_array_elements(p.rules) x WHERE x->>'rule_id' = e->>'rule_id'
   )
)
WHERE p.policy_id = '00000000-0000-0000-0000-0000000d0001';

-- =====================================================================================
-- end 20260614_0006__pii_coverage.sql
-- =====================================================================================
