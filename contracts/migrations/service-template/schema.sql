-- Contract 14 — declarative schema snapshot (reference template).
--
-- This is the current desired state of the `example` service's schema. `atlas schema diff`
-- compares the migration history against this file to detect drift (Contract 14 §3).
--
-- It demonstrates the mandatory tenant-isolation pattern from Contract 13 §4:
--   * a tenant-scoped table with `tenant_id UUID NOT NULL`,
--   * an index that STARTS with tenant_id,
--   * Row Level Security ENABLED + a USING policy keyed on app.tenant_id,
--   * a least-privilege per-service runtime role (Contract 14 §8: RLS + runtime role are
--     part of the service's own schema and are created by the migration Job's DDL user).
--
-- Replace `example` with your service name throughout.

-- ---------------------------------------------------------------------------
-- Schema (owned by this service only — a migration may touch its own schema only).
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS example;

-- ---------------------------------------------------------------------------
-- Least-privilege per-service runtime role (Contract 14 §4, §8).
-- The migration Job's DDL user (granted CREATEROLE on this schema only) creates it.
-- The runtime password is injected from Doppler at db/<service>/runtime_password.
-- NOLOGIN here: the password is set out-of-band; this snapshot only declares the role's grants.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'example_runtime') THEN
    CREATE ROLE example_runtime NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA example TO example_runtime;

-- ---------------------------------------------------------------------------
-- Tenant-scoped table (Contract 13 §4 rule 1).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS example.widgets (
  widget_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID        NOT NULL,
  name        TEXT        NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index that STARTS with tenant_id (Contract 13 §4 rule 1).
CREATE INDEX IF NOT EXISTS ix_widgets_tenant ON example.widgets (tenant_id, created_at);

-- DML grants for the runtime role (least privilege — no DDL).
GRANT SELECT, INSERT, UPDATE, DELETE ON example.widgets TO example_runtime;

-- ---------------------------------------------------------------------------
-- Row Level Security (Contract 13 §4 rule 4).
-- The policy keys on app.tenant_id, set per transaction via `SET LOCAL app.tenant_id = $1`.
-- ---------------------------------------------------------------------------
ALTER TABLE example.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE example.widgets FORCE ROW LEVEL SECURITY;

CREATE POLICY p_widgets_tenant ON example.widgets
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
