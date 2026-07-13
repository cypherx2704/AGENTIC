-- =====================================================================================
-- memory-service — ADDITIVE migration #6 (B4): ACT-R retrieval-frequency counter.
--   PostgreSQL 16.
--
-- NON-BREAKING + IDEMPOTENT: adds ONE column, access_count, to memory.memories. It counts
-- how many times a memory has been RETURNED by a search (the read path bumps it inline,
-- alongside last_accessed_at / last_retrieved_at). It feeds the ACT-R base-level activation
-- (recency x frequency) in the composite re-rank, and is ONLY read when
-- MEMORY_SCORING_DECAY='power_actr'. DEFAULT 0 + NOT NULL, so existing rows and the default
-- exponential-decay path are unaffected.
-- =====================================================================================

ALTER TABLE memory.memories
  ADD COLUMN IF NOT EXISTS access_count BIGINT NOT NULL DEFAULT 0;
