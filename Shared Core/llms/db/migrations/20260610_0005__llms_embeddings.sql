-- =====================================================================================
-- llms-gateway — WP06 embeddings (Phase 3, Amendment Log 2026-06). PostgreSQL 16.
-- Idempotent: safe to re-run top-to-bottom.
--
-- THE blocking deliverable for the RAG + Memory services: POST /v1/embeddings.
--
-- 1) usage_records.operation — parametrize the kind of call a usage row bills
--    ("chat" default | "embedding"); nullable + DEFAULT 'chat' so pre-existing rows
--    and the chat path are unaffected. Threaded through UsageWrite + the Contract-19
--    cypherx.llms.usage.recorded `operation` key.
-- 2) `embed` platform alias (tenant_id IS NULL) -> openai/text-embedding-3-small.
-- 3) provider_pricing seed for text-embedding-3-small: input-token rate only
--    (output rate 0 — embeddings have no completion tokens, output cost 0 by convention).
-- 4) model_capabilities seed for text-embedding-3-small: embedding_dim = 1536, no
--    completion output (max_tokens_cap sentinel 1), 8191-token input context, no
--    vision/tools/streaming.
--
-- All in-code cold-start fallback maps are updated in lockstep (router._PLATFORM_ALIASES
-- / _LITERAL_PROVIDER, cost._FALLBACK_PRICING, capabilities._FALLBACK_CAPABILITIES) —
-- tests/test_config_registry.py parses these seeds and asserts equality so the two can
-- never drift.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS llms;  -- standalone-safe (created in 0001)

-- ── 1) usage_records.operation (nullable, default 'chat' — idempotent) ────────────────
ALTER TABLE llms.usage_records
  ADD COLUMN IF NOT EXISTS operation VARCHAR(20) NOT NULL DEFAULT 'chat';

-- ── 2) `embed` platform alias (tenant_id IS NULL) ─────────────────────────────────────
INSERT INTO llms.model_aliases (tenant_id, alias, model_id, provider)
VALUES
  (NULL, 'embed', 'text-embedding-3-small', 'openai')
ON CONFLICT (alias) WHERE tenant_id IS NULL DO NOTHING;

-- ── 3) provider_pricing: text-embedding-3-small (input-only; output rate 0) ───────────
INSERT INTO llms.provider_pricing
  (provider, model, input_cost_per_1k_tokens, output_cost_per_1k_tokens,
   cached_input_cost_per_1k_tokens, cache_creation_cost_per_1k_tokens, effective_from)
VALUES
  ('openai', 'text-embedding-3-small', 0.00002000, 0.00000000, 0.00000000, 0.00000000, '2026-06-08T00:00:00Z')
ON CONFLICT (provider, model, effective_from) DO NOTHING;

-- ── 4) model_capabilities: text-embedding-3-small (embedding_dim non-NULL) ────────────
INSERT INTO llms.model_capabilities
  (model_id, provider, max_tokens_cap, context_window,
   supports_vision, supports_tools, supports_streaming, embedding_dim)
VALUES
  ('text-embedding-3-small', 'openai', 1, 8191, false, false, false, 1536)
ON CONFLICT (model_id) DO NOTHING;
