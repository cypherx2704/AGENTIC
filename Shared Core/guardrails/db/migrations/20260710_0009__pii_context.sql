-- =====================================================================================
-- guardrails-service — B8: native context-window PII validation -> default-path
-- passport/name. PostgreSQL 16.
--
-- WHY: passport and name today exist only behind off-by-default Presidio spaCy. Real ML NER
-- for names blows the 30/50ms SLO, so the only viable default-path route is deterministic
-- context-gated regex: a passport-number / honorific-name candidate is admitted ONLY when a
-- supporting term appears within N chars (the Presidio context-enhancer / Google DLP hotword
-- mechanism, implemented natively). The code adds `pii-passport-v1` + `pii-name-v1`, both
-- INERT unless `GUARDRAILS_PII_CONTEXT_VALIDATION` is set — the default path stays
-- byte-identical. The registry-consistency gate requires the DB platform rows (else
-- /readyz -> 503) and the platform-default policy must enable them, so this migration adds
-- both. Categories `passport`/`name` already exist in core.redaction PII_CATEGORIES.
--
-- Idempotent: ON CONFLICT DO NOTHING + append-only policy update. Safe to re-run.
-- =====================================================================================

-- ── 1) registry rows (platform; tenant_id IS NULL) ───────────────────────────────────
INSERT INTO guardrails.rules
  (rule_id, tenant_id, version, default_action, default_fail_mode, default_stream_mode,
   default_severity, default_category, direction, timeout_ms, status)
VALUES
  ('pii-passport-v1', NULL, '1', 'redact', 'closed', 'buffer', 'high',   'pii', 'input', 10, 'active'),
  ('pii-name-v1',     NULL, '1', 'redact', 'closed', 'buffer', 'medium', 'pii', 'input', 10, 'active')
ON CONFLICT (rule_id) WHERE tenant_id IS NULL DO NOTHING;

-- ── 2) enable them in the platform-default policy (append only the missing rule_ids) ──
UPDATE guardrails.policies p
SET rules = p.rules || (
  SELECT COALESCE(jsonb_agg(e), '[]'::jsonb)
    FROM jsonb_array_elements(
      '[{"rule_id":"pii-passport-v1","enabled":true,"action_override":null},
        {"rule_id":"pii-name-v1","enabled":true,"action_override":null}]'::jsonb
    ) AS e
   WHERE NOT EXISTS (
     SELECT 1 FROM jsonb_array_elements(p.rules) x WHERE x->>'rule_id' = e->>'rule_id'
   )
)
WHERE p.policy_id = '00000000-0000-0000-0000-0000000d0001';

-- =====================================================================================
-- end 20260710_0009__pii_context.sql
-- =====================================================================================
