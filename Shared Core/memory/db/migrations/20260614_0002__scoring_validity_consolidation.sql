-- =====================================================================================
-- memory-service — ADDITIVE migration #2 (Phase 6 / WP10 follow-on).
--   Retrieval scoring (Generative Agents), temporal validity / supersession, and the
--   consolidation/forgetting audit trail. PostgreSQL 16 + pgvector.
--
-- NON-BREAKING + IDEMPOTENT: only ADDs columns (all NULL-able or DEFAULTed to reproduce
-- today's behavior), indexes, and one new audit table. Re-running this top-to-bottom is
-- safe. None of these are read unless the corresponding feature flag is ON, so a service
-- on the old code path is unaffected.
--
-- New columns on memory.memories:
--   importance_score   — normalized [0,1] write-time importance (DEFAULT 0.5 ≈ neutral).
--   last_retrieved_at  — when this memory was last RETURNED by a search (recency input).
--   valid_until        — temporal validity: NULL = currently valid; set when superseded.
--   superseded_by_id   — the newer memory that supersedes this one (NULL = not superseded).
-- New optional scope columns (richer scoping, additive; defaults keep the 404-anti-leak
-- rule + principal_only/tenant_shared visibility EXACTLY as today):
--   session_scope_id   — optional session this memory is scoped to.
--   agent_scope_id     — optional agent this memory is scoped to.
-- New audit table memory.memory_audit — soft-delete trail for consolidation/forgetting.
-- =====================================================================================

-- ── memories: scoring + validity + richer scope (all additive) ───────────────────────
ALTER TABLE memory.memories
  ADD COLUMN IF NOT EXISTS importance_score   DOUBLE PRECISION NOT NULL DEFAULT 0.5
    CHECK (importance_score >= 0.0 AND importance_score <= 1.0);

ALTER TABLE memory.memories
  ADD COLUMN IF NOT EXISTS last_retrieved_at  TIMESTAMPTZ;

ALTER TABLE memory.memories
  ADD COLUMN IF NOT EXISTS valid_until        TIMESTAMPTZ;

ALTER TABLE memory.memories
  ADD COLUMN IF NOT EXISTS superseded_by_id   UUID;

ALTER TABLE memory.memories
  ADD COLUMN IF NOT EXISTS session_scope_id   VARCHAR(128);

ALTER TABLE memory.memories
  ADD COLUMN IF NOT EXISTS agent_scope_id     VARCHAR(128);

-- Partial index over CURRENTLY-VALID rows: speeds "current only" search (the default when
-- MEMORY_SEARCH_CURRENT_ONLY is on). Rows with valid_until set fall out of the index.
CREATE INDEX IF NOT EXISTS idx_memories_valid
  ON memory.memories (tenant_id, principal_type, principal_id)
  WHERE valid_until IS NULL;

-- Recency input for the composite re-rank (only read when MEMORY_SCORING_ENABLED).
CREATE INDEX IF NOT EXISTS idx_memories_last_retrieved
  ON memory.memories (last_retrieved_at) WHERE last_retrieved_at IS NOT NULL;

-- =====================================================================================
-- memory.memory_audit — soft-delete / forgetting audit trail (consolidation routine).
-- A tenant-scoped table (RLS like the other tenant tables). The consolidation job writes
-- the original memory's snapshot here BEFORE soft-deleting it, so a forget is reversible
-- and explainable. NEVER written unless MEMORY_CONSOLIDATION_ENABLED is on.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS memory.memory_audit (
  id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID         NOT NULL,
  memory_id         UUID         NOT NULL,        -- the original memory (no FK: it is deleted)
  principal_type    VARCHAR(20)  NOT NULL,
  principal_id      VARCHAR(128) NOT NULL,
  action            VARCHAR(32)  NOT NULL,        -- 'consolidated' | 'forgotten' | 'superseded'
  reason            VARCHAR(512),
  summary_memory_id UUID,                          -- the consolidated summary that replaced it
  snapshot          JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- the original row, for replay
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_audit_tenant
  ON memory.memory_audit (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_audit_memory
  ON memory.memory_audit (memory_id);

ALTER TABLE memory.memory_audit ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  EXECUTE 'DROP POLICY IF EXISTS p_memory_audit_tenant ON memory.memory_audit';
  EXECUTE
    'CREATE POLICY p_memory_audit_tenant ON memory.memory_audit FOR ALL '
    'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
    'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)';
END
$$;

GRANT SELECT, INSERT ON memory.memory_audit TO mem_user;
