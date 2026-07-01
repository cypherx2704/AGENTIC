-- =====================================================================================
-- guardrails-service — WP07 custom (tenant-authored) rules (Phase 4b). PostgreSQL 16.
--
-- Extends guardrails.rules (the existing MIXED-scope registry) so a tenant CUSTOM row
-- carries an EXECUTABLE definition, not just metadata. Two custom types:
--   * 'regex'                — `pattern` is a user-supplied regex (validated by the
--                              SAVE-time ReDoS guard in services/rules/custom.py).
--   * 'classifier-threshold' — `classifier_category` + `threshold` compared against the
--                              classifier score for that category.
-- Platform rows (tenant_id IS NULL) keep custom_type = NULL (they are the built-in 11,
-- whose detect() logic lives in code). RLS on guardrails.rules (set in 0001) already
-- admits NULL + own-tenant for read and own-tenant only for write — unchanged here.
--
-- VERSION CHAIN MODEL (append-only, mirrors the policy authoring chain in 0003):
--   * `PUT /v1/rules/{id}` INSERTs a NEW rules row (new rule_id) and RETIRES the old
--     (status='retired') in ONE tenant transaction; published rows are never mutated.
--   * The PUBLIC, STABLE id exposed by the API is `root_rule_id` (the first version's
--     rule_id). `previous_rule_id` links one step back. Exactly one row per
--     (tenant_id, root_rule_id) is non-retired.
--   * The concrete per-version `rule_id` is what the pipeline evaluates (RULES_BY_ID
--     key); minting a fresh rule_id per version keeps the existing
--     uq_rules_id_tenant unique index satisfied.
--
-- Top-to-bottom runnable; fully idempotent (IF NOT EXISTS everywhere). The 0001 grants
-- on guardrails.rules (SELECT/INSERT/UPDATE/DELETE to grd_user) already cover this table;
-- re-affirmed at the foot for standalone runs.
-- =====================================================================================

-- ── rules: executable custom-rule definition + version-chain columns ──────────────────
ALTER TABLE guardrails.rules
  ADD COLUMN IF NOT EXISTS name                VARCHAR(255),               -- human label (custom rows)
  ADD COLUMN IF NOT EXISTS custom_type         VARCHAR(30),                -- NULL=platform; 'regex' | 'classifier-threshold'
  ADD COLUMN IF NOT EXISTS pattern             TEXT,                       -- regex source (regex type)
  ADD COLUMN IF NOT EXISTS classifier_category VARCHAR(50),                -- target category (classifier-threshold type)
  ADD COLUMN IF NOT EXISTS threshold           DOUBLE PRECISION,           -- score threshold (classifier-threshold type)
  ADD COLUMN IF NOT EXISTS root_rule_id        VARCHAR(100),               -- STABLE public id across the version chain
  ADD COLUMN IF NOT EXISTS previous_rule_id    VARCHAR(100),               -- one step back in the chain
  ADD COLUMN IF NOT EXISTS created_by          UUID,                       -- principal agent_id, audit only
  ADD COLUMN IF NOT EXISTS updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- A custom row must be internally consistent for its declared type. Platform rows
-- (custom_type IS NULL) are unconstrained by this CHECK.
ALTER TABLE guardrails.rules DROP CONSTRAINT IF EXISTS ck_rules_custom_definition;
ALTER TABLE guardrails.rules ADD CONSTRAINT ck_rules_custom_definition CHECK (
  custom_type IS NULL
  OR (custom_type = 'regex'                AND pattern IS NOT NULL)
  OR (custom_type = 'classifier-threshold' AND classifier_category IS NOT NULL AND threshold IS NOT NULL)
);

-- Backfill: every pre-existing row is its own root (platform rows + any v1 custom rows).
UPDATE guardrails.rules SET root_rule_id = rule_id WHERE root_rule_id IS NULL;

-- Find a tenant's custom rules fast (the dynamic loader's read), newest first.
CREATE INDEX IF NOT EXISTS idx_rules_tenant_custom
  ON guardrails.rules (tenant_id, status)
  WHERE tenant_id IS NOT NULL;

-- Group a custom rule's version chain by its stable root (GET-by-id + versioned PUT).
CREATE INDEX IF NOT EXISTS idx_rules_root
  ON guardrails.rules (tenant_id, root_rule_id);

-- Exactly ONE non-retired version per logical (tenant, root) custom rule.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rules_active_per_root
  ON guardrails.rules (tenant_id, root_rule_id)
  WHERE tenant_id IS NOT NULL AND status <> 'retired';

-- ── grants (re-affirm idempotently; 0001 already granted these) ───────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON guardrails.rules TO grd_user;
