-- =====================================================================================
-- llms-gateway — rerank + safety-classify surfaces (Phase 3, additive). PostgreSQL 16.
-- Idempotent: safe to re-run top-to-bottom. ADDITIVE & NON-BREAKING:
--   * no column/table/row is removed or renamed; existing chat/embeddings paths unchanged.
--   * usage_records.operation is VARCHAR(20) with NO CHECK constraint, so the new
--     'rerank' / 'classify' values need no schema change — only new alias/pricing/capability
--     reference rows are seeded here.
--
-- Registers the two new endpoints' model aliases + pricing + capabilities:
--   1) `rerank-default` platform alias (tenant_id IS NULL) -> cypherx/rerank-mock-v1.
--   2) `safety-default` platform alias (tenant_id IS NULL) -> cypherx/classify-stub-v1.
--   3) provider_pricing seeds for both: ALL RATES 0 — these surfaces meter by UNITS
--      (Contract-19 search_units / classifications), NOT a per-token cost (no cost rewrite).
--   4) model_capabilities seeds for both: no completion output (max_tokens_cap sentinel 1),
--      no vision/tools/streaming, not embedding models (embedding_dim NULL).
--
-- The default providers are KEYLESS: RERANK_PROVIDER=mock (deterministic lexical scorer)
-- and CLASSIFIER_MODE=stub (deterministic keyword classifier). The 'cypherx' provider key
-- denotes the in-house mock/stub class — no external provider key is involved. A real
-- cross-encoder / safety model is a later additive change behind RERANK_PROVIDER=local /
-- CLASSIFIER_MODE=local and is NOT in the default image.
--
-- All in-code cold-start fallback maps are updated in lockstep (router._PLATFORM_ALIASES
-- / _LITERAL_PROVIDER, cost._FALLBACK_PRICING, capabilities._FALLBACK_CAPABILITIES) —
-- tests/test_config_registry.py parses these seeds and asserts equality so the two can
-- never drift.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS llms;  -- standalone-safe (created in 0001)

-- ── 1+2) platform aliases (tenant_id IS NULL) ─────────────────────────────────────────
INSERT INTO llms.model_aliases (tenant_id, alias, model_id, provider)
VALUES
  (NULL, 'rerank-default', 'rerank-mock-v1',   'cypherx'),
  (NULL, 'safety-default', 'classify-stub-v1', 'cypherx')
ON CONFLICT (alias) WHERE tenant_id IS NULL DO NOTHING;

-- ── 3) provider_pricing: rerank + classify (ALL RATES 0 — metered by UNITS) ───────────
INSERT INTO llms.provider_pricing
  (provider, model, input_cost_per_1k_tokens, output_cost_per_1k_tokens,
   cached_input_cost_per_1k_tokens, cache_creation_cost_per_1k_tokens, effective_from)
VALUES
  ('cypherx', 'rerank-mock-v1',   0.00000000, 0.00000000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z'),
  ('cypherx', 'classify-stub-v1', 0.00000000, 0.00000000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z')
ON CONFLICT (provider, model, effective_from) DO NOTHING;

-- ── 4) model_capabilities: rerank + classify (no completion output; not embeddings) ───
INSERT INTO llms.model_capabilities
  (model_id, provider, max_tokens_cap, context_window,
   supports_vision, supports_tools, supports_streaming, embedding_dim)
VALUES
  ('rerank-mock-v1',   'cypherx', 1, 8192, false, false, false, NULL),
  ('classify-stub-v1', 'cypherx', 1, 8192, false, false, false, NULL)
ON CONFLICT (model_id) DO NOTHING;
