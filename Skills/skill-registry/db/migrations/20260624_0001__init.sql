-- =====================================================================================
-- skill-registry — first-cycle schema (WP11). PostgreSQL 16. Idempotent.
--
-- Run as a superuser / migration role. Creates the `skills` schema, the four registry
-- tables, indexes, Row Level Security (Contract 13), and the grants the runtime role
-- `skill_user` needs.
--
-- TABLES (all MIXED-SCOPE: platform rows tenant_id IS NULL + per-tenant rows):
--   skills, skill_versions, skill_capabilities, skill_health
--
-- THE MARKETPLACE HOLE + ITS FIX:
--   A naive RLS policy uses only `USING (tenant_id = current_tenant OR tenant_id IS NULL)`.
--   That guards READS but NOT writes — a tenant could INSERT/UPDATE a row carrying ANOTHER
--   tenant's tenant_id (or NULL to forge a platform skill). We CLOSE this by splitting each
--   table's policy into a permissive SELECT (read own + platform) and a SEPARATE write
--   policy whose `WITH CHECK (tenant_id = current_tenant)` REJECTS any INSERT/UPDATE that
--   names a tenant_id other than the caller's — on EVERY tenant-scoped table, INCLUDING
--   skill_capabilities. The poller/seed paths run with an EMPTY app.tenant_id and are
--   handled by a dedicated platform policy.
--
-- The runtime role connects and runs every tenant-scoped query inside
--   BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT
-- (the Core in_tenant() helper). It is NOT a superuser and does NOT BYPASSRLS.
--
-- RLS uses the pooled-reset-safe NULLIF(current_setting('app.tenant_id',true),'')::uuid
-- so an empty/unset GUC after a pooled-connection reset never throws on the ''::uuid cast.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS skills;

SET search_path = skills, public;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'skill_user') THEN
    CREATE ROLE skill_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA skills TO skill_user;

-- =====================================================================================
-- TABLES
-- =====================================================================================

-- ── skills (the skill registry) ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skills.skills (
  skill_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID,                       -- NULL = platform skill
  name           VARCHAR(100) NOT NULL,      -- dash-case MCP server name
  status         VARCHAR(20)  NOT NULL DEFAULT 'active',
  latest_version VARCHAR(40),                -- semver of the newest active version
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- Unique per owner: a tenant cannot register two skills of the same name; platform names
-- are unique among platform skills (NULL tenant_id is "distinct" so a partial index pins it).
CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_tenant_name
  ON skills.skills (tenant_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_platform_name
  ON skills.skills (name) WHERE tenant_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_skills_name ON skills.skills (name);

-- ── skill_versions (version chain; retention max 3 active per skill) ────────────────────
CREATE TABLE IF NOT EXISTS skills.skill_versions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID,                          -- NULL = platform; mirrors skills.tenant_id
  skill_id     UUID NOT NULL REFERENCES skills.skills (skill_id) ON DELETE CASCADE,
  version     VARCHAR(40)  NOT NULL,         -- semver
  manifest    JSONB        NOT NULL,         -- Contract-4 MCP manifest
  status      VARCHAR(20)  NOT NULL DEFAULT 'active',  -- active | retired
  created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill ON skills.skill_versions (skill_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_versions_active
  ON skills.skill_versions (skill_id, created_at DESC) WHERE status = 'active';

-- ── skill_capabilities (declared scopes/capabilities per skill) ─────────────────────────
CREATE TABLE IF NOT EXISTS skills.skill_capabilities (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID,                       -- NULL = platform; mirrors skills.tenant_id
  skill_id        UUID NOT NULL REFERENCES skills.skills (skill_id) ON DELETE CASCADE,
  capability     VARCHAR(100) NOT NULL,      -- snake_case skill name (invocable capability)
  required_scope VARCHAR(120) NOT NULL,      -- scope required to invoke it
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (skill_id, capability)
);
CREATE INDEX IF NOT EXISTS idx_skill_caps_skill ON skills.skill_capabilities (skill_id);

-- ── skill_health (manifest poll state) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skills.skill_health (
  skill_id              UUID PRIMARY KEY REFERENCES skills.skills (skill_id) ON DELETE CASCADE,
  tenant_id            UUID,                 -- NULL = platform; mirrors skills.tenant_id
  status               VARCHAR(20) NOT NULL DEFAULT 'active',  -- active | degraded | offline
  last_etag            TEXT,
  consecutive_failures INTEGER     NOT NULL DEFAULT 0,
  last_polled          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_skill_health_status ON skills.skill_health (status);

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — corrected SPLIT policy with WITH CHECK.
-- Every tenant-scoped query runs inside a tx that does
--   SELECT set_config('app.tenant_id','<uuid>',true).
-- The background poller / platform seed run with an EMPTY app.tenant_id.
-- =====================================================================================

ALTER TABLE skills.skills             ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills.skill_versions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills.skill_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills.skill_health       ENABLE ROW LEVEL SECURITY;

-- Reusable predicate fragments (documented; inlined per policy below):
--   current tenant  := NULLIF(current_setting('app.tenant_id', true), '')::uuid
--   own row         := tenant_id = current_tenant
--   platform row    := tenant_id IS NULL
--   platform ctx    := NULLIF(current_setting('app.tenant_id', true), '') IS NULL  (empty GUC)

-- ── skills ─────────────────────────────────────────────────────────────────────────────
-- READ: own rows + platform rows (discovery UNION).
DROP POLICY IF EXISTS p_skills_read ON skills.skills;
CREATE POLICY p_skills_read ON skills.skills FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

-- WRITE (tenant): a tenant may INSERT/UPDATE/DELETE ONLY its own rows. The WITH CHECK
-- half is the marketplace-hole fix — it REJECTS a write that names another tenant_id
-- (or NULL to forge a platform skill).
DROP POLICY IF EXISTS p_skills_write ON skills.skills;
CREATE POLICY p_skills_write ON skills.skills FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- WRITE (platform): the seed/poller run with an EMPTY GUC and may manage platform rows
-- (tenant_id IS NULL) ONLY. Still split USING/WITH CHECK so an empty-GUC path can never
-- touch a tenant's row.
DROP POLICY IF EXISTS p_skills_platform ON skills.skills;
CREATE POLICY p_skills_platform ON skills.skills FOR ALL
  USING      (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── skill_versions ─────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS p_skill_versions_read ON skills.skill_versions;
CREATE POLICY p_skill_versions_read ON skills.skill_versions FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_skill_versions_write ON skills.skill_versions;
CREATE POLICY p_skill_versions_write ON skills.skill_versions FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_skill_versions_platform ON skills.skill_versions;
CREATE POLICY p_skill_versions_platform ON skills.skill_versions FOR ALL
  USING      (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── skill_capabilities (own WITH CHECK policy — explicitly required) ───────────────────
DROP POLICY IF EXISTS p_skill_caps_read ON skills.skill_capabilities;
CREATE POLICY p_skill_caps_read ON skills.skill_capabilities FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_skill_caps_write ON skills.skill_capabilities;
CREATE POLICY p_skill_caps_write ON skills.skill_capabilities FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS p_skill_caps_platform ON skills.skill_capabilities;
CREATE POLICY p_skill_caps_platform ON skills.skill_capabilities FOR ALL
  USING      (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (tenant_id IS NULL
              AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- ── skill_health ───────────────────────────────────────────────────────────────────────
-- READ: own + platform (discovery shows health). WRITE is platform-context only because
-- the manifest poller (empty GUC) owns the health columns; a tenant never writes health
-- directly (only via registration's _init_health, which runs in the tenant tx — covered by
-- the tenant write policy below).
DROP POLICY IF EXISTS p_skill_health_read ON skills.skill_health;
CREATE POLICY p_skill_health_read ON skills.skill_health FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
         OR tenant_id IS NULL);

DROP POLICY IF EXISTS p_skill_health_write ON skills.skill_health;
CREATE POLICY p_skill_health_write ON skills.skill_health FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Platform-context health policy: the poller (empty GUC) updates health for ALL skills,
-- both platform AND tenant-owned. It must therefore be allowed to write tenant rows it
-- does NOT own — but ONLY from the trusted empty-GUC poller context, never from a tenant
-- request (a tenant request always has a non-empty GUC, so this policy never applies to it).
DROP POLICY IF EXISTS p_skill_health_poller ON skills.skill_health;
CREATE POLICY p_skill_health_poller ON skills.skill_health FOR ALL
  USING      (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
  WITH CHECK (NULLIF(current_setting('app.tenant_id', true), '') IS NULL);

-- =====================================================================================
-- GRANTS to the runtime role (skill_user). RLS still applies on top of these.
-- =====================================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON skills.skills             TO skill_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON skills.skill_versions     TO skill_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON skills.skill_capabilities TO skill_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON skills.skill_health       TO skill_user;
