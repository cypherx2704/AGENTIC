-- =====================================================================================
-- memory-service — ADDITIVE migration #4 (B1): halfvec (16-bit) + binary HNSW indexes.
--   PostgreSQL 16 + pgvector >= 0.7 (halfvec / binary_quantize).
--
-- NON-BREAKING + IDEMPOTENT: only ADDs expression HNSW indexes on the EXISTING
-- memory.memory_vectors_1536.embedding column. The base vector(1536) column and its
-- full-precision index (idx_memory_vectors_hnsw) are UNCHANGED, so a service on the old
-- code path (memory_vector_quantization='off') is completely unaffected — it keeps using
-- the vector(1536) index. These indexes are only ever scanned when the flag selects them.
--
-- halfvec expression index: a 2x-smaller HNSW index at ~identical recall (Katz / Neon).
-- The planner uses it when the query casts the same way: `embedding::halfvec(1536) <=> ...`.
--
-- binary expression index (optional 'binary_rerank' tier): a bit(1536) Hamming first pass
-- for the most aggressive memory/latency tier; the code re-ranks that window at full
-- precision, so recall is recovered by the rerank, not the coarse first pass.
-- =====================================================================================

-- halfvec (16-bit) HNSW cosine index — the Tier-1 win (memory_vector_quantization='halfvec').
CREATE INDEX IF NOT EXISTS idx_memory_vectors_hnsw_halfvec
  ON memory.memory_vectors_1536
  USING hnsw ((embedding::halfvec(1536)) halfvec_cosine_ops);

-- binary (1-bit) HNSW Hamming index — the optional aggressive tier
-- (memory_vector_quantization='binary_rerank'; first pass only, full-precision rerank in code).
CREATE INDEX IF NOT EXISTS idx_memory_vectors_hnsw_bit
  ON memory.memory_vectors_1536
  USING hnsw ((binary_quantize(embedding)::bit(1536)) bit_hamming_ops);
