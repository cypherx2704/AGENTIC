-- =====================================================================================
-- guardrails-service — first-cycle seed (Phase 4). PostgreSQL 16.
--
-- Seeds:
--   * guardrails.rules    — the 11 first-cycle platform rule rows (tenant_id IS NULL).
--   * guardrails.policies — exactly ONE platform-default policy (tenant_id IS NULL,
--     is_default = true) whose `rules` JSONB enables all 11 rules at their default action.
--     The fixed policy_id matches PLATFORM_DEFAULT_POLICY_ID in services/policy_engine.py
--     so DB-resolved and built-in policies agree.
--
-- Idempotent: ON CONFLICT DO NOTHING so re-running is safe.
-- =====================================================================================

-- ── rules (platform; tenant_id IS NULL) ───────────────────────────────────────────────
INSERT INTO guardrails.rules
  (rule_id, tenant_id, version, default_action, default_fail_mode, default_stream_mode,
   default_severity, default_category, direction, timeout_ms, status)
VALUES
  -- INPUT rules
  ('prompt-injection-v1',       NULL, '1', 'block',  'closed', 'buffer', 'critical', 'security',  'input',  10, 'active'),
  ('pii-email-v1',              NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'input',  10, 'active'),
  ('pii-phone-v1',              NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'input',  10, 'active'),
  ('pii-credit-card-v1',        NULL, '1', 'block',  'closed', 'buffer', 'high',     'pii',       'input',  10, 'active'),
  ('pii-ssn-v1',                NULL, '1', 'redact', 'closed', 'buffer', 'high',     'pii',       'input',  10, 'active'),
  ('pii-ip-v1',                 NULL, '1', 'redact', 'closed', 'buffer', 'low',      'pii',       'input',  10, 'active'),
  ('pii-address-v1',            NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'input',  10, 'active'),
  ('jailbreak-v1',              NULL, '1', 'block',  'closed', 'buffer', 'critical', 'jailbreak', 'input',  10, 'active'),
  ('toxicity-v1',               NULL, '1', 'block',  'closed', 'buffer', 'high',     'toxicity',  'input',  50, 'active'),
  -- OUTPUT rules
  ('output-pii-email-v1',       NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'output', 10, 'active'),
  ('output-pii-credit-card-v1', NULL, '1', 'block',  'closed', 'buffer', 'high',     'pii',       'output', 10, 'active'),
  ('output-pii-phone-v1',       NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'output', 10, 'active'),
  ('output-pii-ssn-v1',         NULL, '1', 'redact', 'closed', 'buffer', 'high',     'pii',       'output', 10, 'active'),
  ('output-pii-ip-v1',          NULL, '1', 'redact', 'closed', 'buffer', 'low',      'pii',       'output', 10, 'active'),
  ('output-pii-address-v1',     NULL, '1', 'redact', 'closed', 'buffer', 'medium',   'pii',       'output', 10, 'active'),
  ('output-jailbreak-leak-v1',  NULL, '1', 'block',  'closed', 'buffer', 'high',     'jailbreak', 'output', 10, 'active'),
  ('output-toxicity-v1',        NULL, '1', 'block',  'closed', 'buffer', 'high',     'toxicity',  'output', 50, 'active'),
  ('output-max-length-v1',      NULL, '1', 'block',  'closed', 'buffer', 'low',      'length',    'output', 10, 'active')
ON CONFLICT (rule_id) WHERE tenant_id IS NULL DO NOTHING;

-- ── policies (platform default; tenant_id IS NULL, is_default = true) ───────────────────
INSERT INTO guardrails.policies (policy_id, tenant_id, name, version, status, rules, is_default)
VALUES (
  '00000000-0000-0000-0000-0000000d0001',
  NULL,
  'Platform Default Policy',
  1,
  'active',
  '[
    {"rule_id": "prompt-injection-v1",       "enabled": true, "action_override": null},
    {"rule_id": "pii-email-v1",              "enabled": true, "action_override": null},
    {"rule_id": "pii-phone-v1",              "enabled": true, "action_override": null},
    {"rule_id": "pii-credit-card-v1",        "enabled": true, "action_override": null},
    {"rule_id": "pii-ssn-v1",                "enabled": true, "action_override": null},
    {"rule_id": "pii-ip-v1",                 "enabled": true, "action_override": null},
    {"rule_id": "pii-address-v1",            "enabled": true, "action_override": null},
    {"rule_id": "jailbreak-v1",              "enabled": true, "action_override": null},
    {"rule_id": "toxicity-v1",               "enabled": true, "action_override": null},
    {"rule_id": "output-pii-email-v1",       "enabled": true, "action_override": null},
    {"rule_id": "output-pii-credit-card-v1", "enabled": true, "action_override": null},
    {"rule_id": "output-pii-phone-v1",       "enabled": true, "action_override": null},
    {"rule_id": "output-pii-ssn-v1",         "enabled": true, "action_override": null},
    {"rule_id": "output-pii-ip-v1",          "enabled": true, "action_override": null},
    {"rule_id": "output-pii-address-v1",     "enabled": true, "action_override": null},
    {"rule_id": "output-jailbreak-leak-v1",  "enabled": true, "action_override": null},
    {"rule_id": "output-toxicity-v1",        "enabled": true, "action_override": null},
    {"rule_id": "output-max-length-v1",      "enabled": true, "action_override": null}
  ]'::jsonb,
  true
)
ON CONFLICT (policy_id) DO NOTHING;
