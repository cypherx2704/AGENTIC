-- =====================================================================================
-- memory-service — ADDITIVE migration #7 (B7): associative memory links (edge table).
--   PostgreSQL 16.
--
-- NON-BREAKING + IDEMPOTENT: adds ONE new tenant-scoped table, memory.memory_links, an
-- RLS-scoped directed edge set between memories of the SAME principal (Zettelkasten-style
-- associations). Written at ingest ONLY when MEMORY_LINKING_ENABLED is on; read at
-- retrieval by the bounded 1-hop link expansion. No existing table is touched, so a service
-- on the old code path is unaffected.
--
-- RLS mirrors the other tenant tables EXACTLY (USING/WITH CHECK on app.tenant_id), so the
-- edge table can never leak across tenants. Ownership columns (principal_type/id) let the
-- expansion re-apply the same principal visibility the memories table enforces. Both
-- endpoints CASCADE-delete with their memory rows so a delete / GDPR wipe removes edges too.
-- =====================================================================================

CREATE TABLE IF NOT EXISTS memory.memory_links (
  id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID         NOT NULL,
  principal_type  VARCHAR(20)  NOT NULL,   -- the OWNER of both endpoints (links are intra-principal)
  principal_id    VARCHAR(128) NOT NULL,
  src_memory_id   UUID         NOT NULL REFERENCES memory.memories(id) ON DELETE CASCADE,
  dst_memory_id   UUID         NOT NULL REFERENCES memory.memories(id) ON DELETE CASCADE,
  relation        VARCHAR(32)  NOT NULL DEFAULT 'associated',  -- link attribute / kind
  weight          DOUBLE PRECISION NOT NULL DEFAULT 1.0,       -- association strength (cosine)
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, src_memory_id, dst_memory_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_links_src
  ON memory.memory_links (tenant_id, src_memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_links_dst
  ON memory.memory_links (tenant_id, dst_memory_id);

ALTER TABLE memory.memory_links ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  EXECUTE 'DROP POLICY IF EXISTS p_memory_links_tenant ON memory.memory_links';
  EXECUTE
    'CREATE POLICY p_memory_links_tenant ON memory.memory_links FOR ALL '
    'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
    'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)';
END
$$;

GRANT SELECT, INSERT, DELETE ON memory.memory_links TO mem_user;
