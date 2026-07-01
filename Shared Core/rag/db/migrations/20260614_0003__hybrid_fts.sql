-- =====================================================================================
-- rag-service — ADDITIVE migration: hybrid (dense + lexical) full-text search support.
-- Phase 5 / WP09 follow-up. PostgreSQL 16 (no pgvector dependency in this file).
--
-- Adds a Postgres-native lexical retrieval leg to rag.chunks so the query path can fuse a
-- dense (pgvector) ranking with a lexical (tsvector / ts_rank_cd) ranking via Reciprocal
-- Rank Fusion. This is ENTIRELY ADDITIVE and changes NO existing behaviour:
--   * the default search_mode stays 'dense' (the two-pass HNSW CTE is untouched);
--   * the new column is GENERATED ALWAYS so it is auto-maintained on every INSERT/UPDATE —
--     no application write path changes and no backfill job is required (existing rows are
--     populated by Postgres when the column is added);
--   * a GIN index makes the websearch_to_tsquery @@ search index-friendly.
--
-- The lexical text combines the chunk content with an OPTIONAL contextual prefix carried in
-- metadata->>'context' (see RAG_CONTEXTUAL_INGEST). When that key is absent the expression
-- is just to_tsvector('english', content) — identical lexical signal to a plain chunk, so
-- enabling contextual ingest later only ADDS signal and never rewrites existing rows.
--
-- Run as a superuser / migration role (same as 0001/0002). Idempotent.
-- =====================================================================================

-- ── Generated tsvector column on rag.chunks ───────────────────────────────────────────
-- English config; content is the primary signal, the optional contextual prefix is folded
-- in at weight 'B' so it nudges-but-does-not-dominate lexical ranking. GENERATED ALWAYS so
-- Postgres maintains it transparently (no app write, no trigger, no backfill).
ALTER TABLE rag.chunks
  ADD COLUMN IF NOT EXISTS content_tsv tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(metadata->>'context', '')), 'B') ||
    setweight(to_tsvector('english', coalesce(content, '')), 'A')
  ) STORED;

-- ── GIN index for the @@ websearch_to_tsquery lexical leg ──────────────────────────────
CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv
  ON rag.chunks USING gin (content_tsv);

-- The runtime role already has SELECT/INSERT/UPDATE/DELETE on rag.chunks (granted in 0001);
-- a GENERATED column needs no extra grant (it is never written explicitly by the app).
