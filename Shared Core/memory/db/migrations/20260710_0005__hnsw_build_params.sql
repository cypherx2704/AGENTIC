-- =====================================================================================
-- memory-service — ADDITIVE migration #5 (B3): explicit HNSW build params (m / ef_construction).
--   PostgreSQL 16 + pgvector.
--
-- The original index (migration #1) built the full-precision HNSW index with UNSPECIFIED
-- WITH (...) params, so it inherited pgvector's defaults (m=16, ef_construction=64). This
-- migration REBUILDS it WITH those params made EXPLICIT so an operator can tune graph
-- quality by editing them here. The literal values MIRROR the config defaults
-- (memory_hnsw_m=16, memory_hnsw_ef_construction=64), so rebuilding to them is
-- behavior-preserving: query recall/latency at the default ef_search (40) is unchanged.
--
-- Raise m (16..32) / ef_construction (128..256) here for a higher-quality graph (a
-- one-time build cost, zero query cost). The per-query ef_search knob is set at runtime
-- via `SET LOCAL hnsw.ef_search` (config memory_hnsw_ef_search; default 0 => not emitted).
--
-- IDEMPOTENT: DROP IF EXISTS then CREATE the same-named index WITH explicit params.
-- =====================================================================================

DROP INDEX IF EXISTS memory.idx_memory_vectors_hnsw;
CREATE INDEX IF NOT EXISTS idx_memory_vectors_hnsw
  ON memory.memory_vectors_1536
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
