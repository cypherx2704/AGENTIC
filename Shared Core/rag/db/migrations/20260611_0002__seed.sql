-- =====================================================================================
-- rag-service — seeds (Phase 5 / WP09). Idempotent.
--
-- 1) rag.pricing admin-managed unit-cost knobs (units-only metering; billing rollup joins
--    these downstream — RAG never computes cost in the hot path, Contract-14 single-owner).
--
-- NOTE: this seed intentionally touches ONLY the `rag` schema. The service-token allow-list
-- (`auth.service_acl`) is OWNED and seeded by auth-service — the `rag` caller edges
-- (rag→auth-service, rag→llms-gateway) are provisioned by auth's
-- `20260614_0009__service_acl_seed.sql`. RAG must NOT write into the `auth` schema: a
-- cross-schema seed here coupled rag to auth's column names and broke the migrate job when
-- auth renamed the columns (source_service/scopes → caller_service/allowed_scopes).
-- =====================================================================================

-- ── RAG unit-cost knobs (zero defaults — operators tune; presence is what matters) ──
INSERT INTO rag.pricing (unit, unit_cost, currency) VALUES
  ('embedding_cost', 0.000000000000, 'USD'),
  ('storage_cost',   0.000000000000, 'USD'),
  ('ocr_cost',       0.000000000000, 'USD'),
  ('query_cost',     0.000000000000, 'USD'),
  ('rerank_cost',    0.000000000000, 'USD')
ON CONFLICT (unit) DO NOTHING;
