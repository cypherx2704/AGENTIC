-- =====================================================================================
-- guardrails-service — B3: ICAO 9303 MRZ passport detection (regex + check digits).
-- PostgreSQL 16.
--
-- WHY: passport detection previously existed ONLY behind the off-by-default Presidio
-- US_PASSPORT recognizer (a bare number regex, no checksum, requires spaCy). The code adds
-- a self-contained MRZ detector (TD1/TD2/TD3 fixed-width regex + the 7-3-1 mod-10 check
-- digits over the document number, DOB, expiry, and composite field) that runs on the
-- DEFAULT hot path. Category `passport` already exists in core.redaction PII_CATEGORIES, so
-- redaction/HMAC needs no change. The registry-consistency gate (/readyz) requires the DB
-- `guardrails.rules` platform rows to mirror the built-ins, and the platform-default policy
-- must ENABLE them for them to run — this migration adds both. Ships default-on like
-- Luhn/SSN (four independent check digits => astronomically-unlikely false positives).
--
-- Idempotent: rules INSERT uses ON CONFLICT DO NOTHING; the policy UPDATE appends only the
-- rule_ids not already present. Safe to re-run.
-- =====================================================================================

-- ── 1) registry rows (platform; tenant_id IS NULL) ───────────────────────────────────
INSERT INTO guardrails.rules
  (rule_id, tenant_id, version, default_action, default_fail_mode, default_stream_mode,
   default_severity, default_category, direction, timeout_ms, status)
VALUES
  ('pii-passport-mrz-v1',        NULL, '1', 'redact', 'closed', 'buffer', 'high', 'pii', 'input',  10, 'active'),
  ('output-pii-passport-mrz-v1', NULL, '1', 'redact', 'closed', 'buffer', 'high', 'pii', 'output', 10, 'active')
ON CONFLICT (rule_id) WHERE tenant_id IS NULL DO NOTHING;

-- ── 2) enable them in the platform-default policy (append only the missing rule_ids) ──
UPDATE guardrails.policies p
SET rules = p.rules || (
  SELECT COALESCE(jsonb_agg(e), '[]'::jsonb)
    FROM jsonb_array_elements(
      '[{"rule_id":"pii-passport-mrz-v1","enabled":true,"action_override":null},
        {"rule_id":"output-pii-passport-mrz-v1","enabled":true,"action_override":null}]'::jsonb
    ) AS e
   WHERE NOT EXISTS (
     SELECT 1 FROM jsonb_array_elements(p.rules) x WHERE x->>'rule_id' = e->>'rule_id'
   )
)
WHERE p.policy_id = '00000000-0000-0000-0000-0000000d0001';

-- =====================================================================================
-- end 20260710_0007__passport_mrz.sql
-- =====================================================================================
