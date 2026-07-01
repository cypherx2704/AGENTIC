-- =====================================================================================
-- cypherx-a1 Phase B — active memory (consolidation) + the change/activity surface (additive).
--
-- Widens two CHECK enumerations (additive — existing rows stay valid):
--   entities.kind   += 'change' (a discrete change event: a commit, or a PR/ticket-transition
--                       when commit granularity is unavailable),
--                    += 'capability' / 'expertise_summary' (consolidation/reflection summaries).
--   edges.rel       += 'touched'    (change -> repo/file it changed),
--                    += 'summarizes' (a summary entity -> the evidence it was synthesized from).
-- Adds a time index for the activity-timeline query. No RLS change.
-- =====================================================================================

ALTER TABLE cypherx_a1.entities DROP CONSTRAINT IF EXISTS entities_kind_enum;
ALTER TABLE cypherx_a1.entities ADD CONSTRAINT entities_kind_enum CHECK (kind IN
  ('person','service','repo','feature','decision','incident','pr','ticket','document',
   'change','capability','expertise_summary'));

ALTER TABLE cypherx_a1.edges DROP CONSTRAINT IF EXISTS edges_rel_enum;
ALTER TABLE cypherx_a1.edges ADD CONSTRAINT edges_rel_enum CHECK (rel IN
  ('owns','authored','reviewed','depends_on','caused','resolved','mentions',
   'decided_in','deployed','expert_in','part_of','touched','summarizes'));

-- Activity-timeline: current change/pr/ticket/incident nodes ordered by time within a tenant.
CREATE INDEX IF NOT EXISTS idx_entities_activity
  ON cypherx_a1.entities (tenant_id, valid_from DESC)
  WHERE valid_to IS NULL AND kind IN ('change','pr','ticket','incident');
