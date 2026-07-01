-- =====================================================================================
-- llms-gateway — WP02 cross-service foundations (Phase 3, Amendment Log 2026-06).
-- PostgreSQL 16. Idempotent: safe to re-run top-to-bottom.
--
-- 1) Billing uniqueness key -> gateway-minted `llm_call_id` (amended fix #3):
--      * usage_records.llm_call_id UUID NOT NULL (backfilled for pre-existing rows)
--      * UNIQUE (tenant_id, llm_call_id) — THE billing uniqueness key
--      * request_id demoted to a NON-unique correlation column (Contract 8: one
--        upstream X-Request-ID legitimately spans multiple LLM calls — both bill)
--      * uq_usage_tenant_request dropped; idx_usage_request_id added
-- 2) `llms.model_capabilities` (Component 2 — pulled into Phase 3, de-hardcoding
--    amendment): DB is the single authority for model capabilities; seeded here.
-- 3) Seed reconciliation (audit-found drift): platform aliases `code` + `vision`
--    added so the in-code cold-start fallback maps == the seeds.
-- =====================================================================================

-- ── 1) usage_records: llm_call_id billing key ────────────────────────────────────────
ALTER TABLE llms.usage_records ADD COLUMN IF NOT EXISTS llm_call_id UUID;

-- Backfill rows written before this migration (each pre-existing row WAS one call).
UPDATE llms.usage_records SET llm_call_id = gen_random_uuid() WHERE llm_call_id IS NULL;

ALTER TABLE llms.usage_records ALTER COLUMN llm_call_id SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'uq_usage_llm_call' AND conrelid = 'llms.usage_records'::regclass
  ) THEN
    ALTER TABLE llms.usage_records
      ADD CONSTRAINT uq_usage_llm_call UNIQUE (tenant_id, llm_call_id);
  END IF;
END
$$;

-- request_id is correlation-only from here on: non-unique index, old constraint gone.
ALTER TABLE llms.usage_records DROP CONSTRAINT IF EXISTS uq_usage_tenant_request;
CREATE INDEX IF NOT EXISTS idx_usage_request_id ON llms.usage_records (request_id);

-- ── 2) model_capabilities (platform-scoped — no tenant_id, no RLS) ───────────────────
CREATE TABLE IF NOT EXISTS llms.model_capabilities (
  model_id           VARCHAR(100) PRIMARY KEY,
  provider           VARCHAR(50)  NOT NULL,
  max_tokens_cap     INTEGER      NOT NULL,   -- hard provider-side completion cap
  context_window     INTEGER      NOT NULL,
  supports_vision    BOOLEAN      NOT NULL DEFAULT false,
  supports_tools     BOOLEAN      NOT NULL DEFAULT true,
  supports_streaming BOOLEAN      NOT NULL DEFAULT true,
  embedding_dim      INTEGER,                 -- non-NULL for embedding models (WP06)
  updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

GRANT SELECT ON llms.model_capabilities TO llms_user;

-- Seed: current first-cycle catalog (updates PR-only; opus id reconciled per
-- Amendment Log — claude-opus-4-7 -> claude-opus-4-8).
INSERT INTO llms.model_capabilities
  (model_id, provider, max_tokens_cap, context_window,
   supports_vision, supports_tools, supports_streaming, embedding_dim)
VALUES
  ('claude-opus-4-8',   'anthropic', 32000, 200000, true, true, true, NULL),
  ('claude-sonnet-4-6', 'anthropic',  8192, 200000, true, true, true, NULL),
  ('claude-haiku-4-5',  'anthropic',  8192, 200000, true, true, true, NULL),
  ('gpt-4o',            'openai',    16384, 128000, true, true, true, NULL),
  ('gpt-4o-mini',       'openai',    16384, 128000, true, true, true, NULL)
ON CONFLICT (model_id) DO NOTHING;

-- ── 3) Seed reconciliation: `code` + `vision` platform aliases ───────────────────────
INSERT INTO llms.model_aliases (tenant_id, alias, model_id, provider)
VALUES
  (NULL, 'code',    'claude-sonnet-4-6', 'anthropic'),
  (NULL, 'vision',  'claude-sonnet-4-6', 'anthropic')
ON CONFLICT (alias) WHERE tenant_id IS NULL DO NOTHING;
