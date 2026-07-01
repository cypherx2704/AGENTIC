-- =====================================================================================
-- cypherx-a1 Phase KG — knowledge-graph ACCURACY (additive, idempotent). PostgreSQL 16.
--
-- Raises graph accuracy with FOUR additive, app-owned upgrades grounded in the temporal-KG
-- + coreference + schema-guided-extraction literature (Zep/Graphiti, LINK-KG, ontology-
-- constrained extraction). NO new pg extension (cxa1_user cannot CREATE EXTENSION), no enum
-- removal, no RLS removal, no rewrite of existing rows. Re-runnable.
--
--   1) BITEMPORAL edges. The init schema already carries valid_from / valid_to (transaction
--      time, NULL ⇒ current). This adds the explicit BItemporal columns so a contradiction
--      is recorded as a fact-time close + an ingest-time stamp, not just a valid_to close:
--        * valid_until      — fact-time end of validity (mirrors/augments valid_to; the world
--                             stopped being this way at this instant).
--        * ingested_at      — when this edge was written (defaults to created_at semantics).
--        * invalidated_at   — when this edge was superseded/contradicted (system time of the
--                             close). NULL ⇒ never invalidated.
--      Default graph reads keep filtering valid_to IS NULL (current slice). History / as-of
--      reads are an ADDITIVE query param, never the default.
--
--   2) EXTRACTION QA per edge:
--        * source_span            — the source text span (quote/offsets) the fact came from.
--        * extraction_confidence  — the extractor's own confidence (distinct from the graph
--                                   `confidence` weight; lets QA drop/flag below a threshold
--                                   without losing the deterministic-ingest confidence).
--
--   3) ENTITY RESOLUTION / canonicalization. A mention -> canonical entity map with type-
--      aware coreference ('J. Smith' / 'John Smith' -> one entity). Edges are redirected to
--      the canonical id at resolve time; the mention rows are PRESERVED for audit. RLS-scoped.
--
--   4) SCHEMA/ONTOLOGY-GUIDED EXTRACTION is enforced APP-SIDE (kg/schema.py) — no DB change
--      needed beyond the existing rel/kind CHECK enums; this migration only records the QA
--      columns the extractor populates.
-- =====================================================================================

-- ── 1) + 2) Bitemporal + extraction-QA columns on edges (all nullable / defaulted) ────────
ALTER TABLE cypherx_a1.edges
  ADD COLUMN IF NOT EXISTS valid_until           TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS ingested_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS invalidated_at        TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS source_span           TEXT,
  ADD COLUMN IF NOT EXISTS extraction_confidence NUMERIC(4,3);

-- As-of / history reads filter on the fact-time window. A partial index over closed edges
-- keeps an as-of scan (valid_until present) cheap without touching the hot current-slice path.
CREATE INDEX IF NOT EXISTS idx_edges_valid_until
  ON cypherx_a1.edges (tenant_id, src_entity_id, rel, valid_until)
  WHERE valid_until IS NOT NULL;

-- ── 3) entity_mentions — mention -> canonical entity map (type-aware coreference) ─────────
-- A "mention" is a raw surface form observed for an entity (e.g. 'J. Smith'). The resolver
-- maps it to a canonical entity_id; edges that referenced the mention's own (now-merged)
-- entity are redirected to the canonical id, and the mention row is retained for audit.
CREATE TABLE IF NOT EXISTS cypherx_a1.entity_mentions (
  mention_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           UUID         NOT NULL,
  kind                VARCHAR(20)  NOT NULL,                 -- entity kind the mention is of
  surface_form        TEXT         NOT NULL,                 -- the raw observed name/key
  normalized_form     TEXT         NOT NULL,                 -- normalized for coreference match
  canonical_entity_id UUID         NOT NULL,                 -- the entity it resolved to
  source              VARCHAR(40)  NOT NULL DEFAULT 'resolver',
  resolver            VARCHAR(40)  NOT NULL DEFAULT 'exact', -- exact | coref | handle | manual
  confidence          NUMERIC(4,3) NOT NULL DEFAULT 1.000,
  created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_entity_mentions UNIQUE (tenant_id, kind, normalized_form)
);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_tenant
  ON cypherx_a1.entity_mentions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_canonical
  ON cypherx_a1.entity_mentions (tenant_id, canonical_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_lookup
  ON cypherx_a1.entity_mentions (tenant_id, kind, normalized_form);

-- ── RLS (Contract 13) — enable + FORCE on the new tenant-scoped table ─────────────────────
ALTER TABLE cypherx_a1.entity_mentions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cypherx_a1.entity_mentions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_mentions_isolation ON cypherx_a1.entity_mentions;
CREATE POLICY entity_mentions_isolation ON cypherx_a1.entity_mentions FOR ALL
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- ── Grants to the runtime role (RLS still applies on top) ─────────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.entity_mentions TO cxa1_user;
