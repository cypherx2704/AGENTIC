-- =====================================================================================
-- rag-service — seeds (Phase 5 / WP09). Idempotent.
--
-- 1) rag.pricing admin-managed unit-cost knobs (units-only metering; billing rollup joins
--    these downstream — RAG never computes cost in the hot path, Contract-14 single-owner).
-- 2) auth.service_acl extension (cross-phase Phase 2 update): rag-service must be allowed to
--    call llms-gateway (embeddings) + auth-service (service-token mint + JWKS). Guarded so it
--    is a no-op when the auth schema/table is absent (standalone rag DB).
-- =====================================================================================

-- ── 1) RAG unit-cost knobs (zero defaults — operators tune; presence is what matters) ──
INSERT INTO rag.pricing (unit, unit_cost, currency) VALUES
  ('embedding_cost', 0.000000000000, 'USD'),
  ('storage_cost',   0.000000000000, 'USD'),
  ('ocr_cost',       0.000000000000, 'USD'),
  ('query_cost',     0.000000000000, 'USD'),
  ('rerank_cost',    0.000000000000, 'USD')
ON CONFLICT (unit) DO NOTHING;

-- ── 2) Service-ACL edges (only if the auth.service_acl table exists) ───────────────────
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
     WHERE table_schema = 'auth' AND table_name = 'service_acl'
  ) THEN
    -- rag-service -> llms-gateway (embeddings) ; rag-service -> auth-service (token + JWKS)
    INSERT INTO auth.service_acl (source_service, target_service, scopes)
    VALUES
      ('rag-service', 'llms-gateway', ARRAY['internal:read','internal:write']),
      ('rag-service', 'auth-service', ARRAY['internal:read'])
    ON CONFLICT DO NOTHING;
  END IF;
END
$$;
