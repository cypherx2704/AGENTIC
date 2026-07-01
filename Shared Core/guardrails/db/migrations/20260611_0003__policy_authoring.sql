-- =====================================================================================
-- guardrails-service — WP07 policy authoring + simulation (Phase 4b). PostgreSQL 16.
--
-- Adds the append-only version-chain plumbing to guardrails.policies, a
-- fail-mode/policy audit log, and (re)affirms the grants the runtime role needs for
-- policy CRUD + agent assignment. Top-to-bottom runnable; fully idempotent.
--
-- VERSION CHAIN MODEL (amended plan — append-only):
--   * Every edit INSERTs a NEW policies row (a new version) and NEVER mutates the
--     published rules of an existing row. The chain is linked two ways:
--       - previous_policy_id -> the row this version superseded (already in 0001).
--       - root_policy_id     -> the FIRST version's policy_id; STABLE across all edits.
--     The PUBLIC policy id exposed by the API is root_policy_id, so `GET /v1/policies/{id}`
--     and agent assignment keep working across edits.
--   * Exactly one row per (root_policy_id) has status='active'; older versions become
--     'superseded'. The atomic repoint (supersede old + activate new) runs in ONE
--     tenant transaction (api/policies.py).
--   * agent_policies.policy_id stores the ROOT id; the resolver (services/policy_engine.py)
--     joins to the active version by root, so an edit auto-flips every assigned agent.
--
-- fail_mode_override changes are AUDITED here (policy_audit row) AND via the outbox
-- (cypherx.guardrails.policy.changed) — see api/policies.py / db/outbox.py.
-- =====================================================================================

-- ── policies: stable logical id across the append-only version chain ──────────────────
ALTER TABLE guardrails.policies
  ADD COLUMN IF NOT EXISTS root_policy_id     UUID,
  ADD COLUMN IF NOT EXISTS stream_mode        VARCHAR(20) NOT NULL DEFAULT 'buffer',
  ADD COLUMN IF NOT EXISTS fail_mode_override VARCHAR(20),  -- NULL = use each rule's default_fail_mode
  ADD COLUMN IF NOT EXISTS created_by         UUID;          -- principal agent_id, audit only

-- Backfill root_policy_id for any pre-existing rows (v1 rows are their own root).
UPDATE guardrails.policies SET root_policy_id = policy_id WHERE root_policy_id IS NULL;

-- Group the chain by root; the resolver + GET-by-id key off this.
CREATE INDEX IF NOT EXISTS idx_policies_root
  ON guardrails.policies (root_policy_id, version DESC);

-- Exactly ONE active version per logical (root) policy.
CREATE UNIQUE INDEX IF NOT EXISTS uq_policies_active_per_root
  ON guardrails.policies (root_policy_id)
  WHERE status = 'active';

-- ── policy_audit — append-only audit of policy state changes (fail_mode_override etc.) ──
-- TENANT-SCOPED (RLS). One row per audited change: create / edit / assign / fail_mode_override.
CREATE TABLE IF NOT EXISTS guardrails.policy_audit (
  id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID         NOT NULL,
  root_policy_id  UUID         NOT NULL,
  policy_id       UUID         NOT NULL,              -- the concrete version this audit refers to
  action          VARCHAR(40)  NOT NULL,              -- created | edited | assigned | fail_mode_override_changed
  actor_agent_id  UUID,                               -- principal agent_id (NULL for service-only)
  request_id      UUID,
  trace_id        UUID,
  details         JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- {old, new, ...} — redaction-safe metadata only
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_policy_audit_root
  ON guardrails.policy_audit (root_policy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_policy_audit_tenant
  ON guardrails.policy_audit (tenant_id, created_at DESC);

ALTER TABLE guardrails.policy_audit ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_policy_audit_tenant ON guardrails.policy_audit;
CREATE POLICY p_policy_audit_tenant ON guardrails.policy_audit FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- policy_audit: append-only — INSERT + SELECT only (no UPDATE/DELETE at runtime).
GRANT SELECT, INSERT ON guardrails.policy_audit TO grd_user;

-- policies / agent_policies grants already cover INSERT/UPDATE (0001); re-affirm idempotently.
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.policies      TO grd_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.agent_policies TO grd_user;

-- Seeded platform-default policy: make it its own root (v1) so it reads back uniformly.
UPDATE guardrails.policies
   SET root_policy_id = policy_id
 WHERE policy_id = '00000000-0000-0000-0000-0000000d0001'
   AND root_policy_id IS NULL;
