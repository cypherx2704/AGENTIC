-- =====================================================================================
-- llms-gateway — first-cycle seed (Phase 3). PostgreSQL 16.
--
-- Seeds:
--   * provider_pricing — Anthropic (opus/sonnet/haiku) + OpenAI (gpt-4o / gpt-4o-mini)
--     with plausible per-1k-token USD rates (incl. cached + cache-creation for Anthropic).
--   * model_aliases    — platform defaults (tenant_id IS NULL):
--       fast  -> claude-haiku-4-5   (anthropic)
--       smart -> claude-sonnet-4-6  (anthropic)
--       default -> claude-sonnet-4-6 (anthropic)
--
-- Idempotent: ON CONFLICT DO NOTHING so re-running is safe.
-- =====================================================================================

-- ── provider_pricing ─────────────────────────────────────────────────────────────────
INSERT INTO llms.provider_pricing
  (provider, model, input_cost_per_1k_tokens, output_cost_per_1k_tokens,
   cached_input_cost_per_1k_tokens, cache_creation_cost_per_1k_tokens, effective_from)
VALUES
  ('anthropic', 'claude-opus-4-8',   0.01500000, 0.07500000, 0.00150000, 0.01875000, '2026-06-08T00:00:00Z'),
  ('anthropic', 'claude-sonnet-4-6', 0.00300000, 0.01500000, 0.00030000, 0.00375000, '2026-06-08T00:00:00Z'),
  ('anthropic', 'claude-haiku-4-5',  0.00080000, 0.00400000, 0.00008000, 0.00100000, '2026-06-08T00:00:00Z'),
  ('openai',    'gpt-4o',            0.00500000, 0.01500000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z'),
  ('openai',    'gpt-4o-mini',       0.00015000, 0.00060000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z')
ON CONFLICT (provider, model, effective_from) DO NOTHING;

-- ── model_aliases (platform defaults — tenant_id IS NULL) ──────────────────────────────
INSERT INTO llms.model_aliases (tenant_id, alias, model_id, provider)
VALUES
  (NULL, 'fast',    'claude-haiku-4-5',  'anthropic'),
  (NULL, 'smart',   'claude-sonnet-4-6', 'anthropic'),
  (NULL, 'default', 'claude-sonnet-4-6', 'anthropic')
ON CONFLICT (alias) WHERE tenant_id IS NULL DO NOTHING;
