-- =====================================================================================
-- cypherx-a1 Phase A — confidence-weighted, bitemporally-auditable retrieval (additive).
--
-- Adds an explicit supersede LINK between a closed edge and the edge that replaced it, so a
-- contradiction (e.g. ownership reassigned, an extracted relation revised when the source
-- artifact changes) is auditable as a chain rather than an unlinked valid_to close. Grounded
-- in Zep/Graphiti bi-temporal invalidation. Idempotent; no enum/RLS changes.
-- =====================================================================================

ALTER TABLE cypherx_a1.edges
  ADD COLUMN IF NOT EXISTS supersedes_edge_id UUID;

-- Walk a supersede chain (new -> the edge it replaced) within a tenant.
CREATE INDEX IF NOT EXISTS idx_edges_supersedes
  ON cypherx_a1.edges (tenant_id, supersedes_edge_id) WHERE supersedes_edge_id IS NOT NULL;
