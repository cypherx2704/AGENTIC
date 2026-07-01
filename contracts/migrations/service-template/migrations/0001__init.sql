-- Contract 14 — initial versioned migration (reference template).
--
-- Versioned migration naming: <timestamp>__<name>.sql (Contract 14 §2). This template uses
-- the sequence number `0001` for clarity; a real service uses a sortable timestamp, e.g.
-- `20260522_0900__init.sql`.
--
-- This migration brings an empty database up to the state declared in ../schema.sql.
-- It is an EXPAND-only migration (Contract 14 §5): create-only, no destructive DDL.
-- It is applied by the migration Job's DDL user (Contract 14 §4) and creates the per-service
-- runtime role + RLS, which are part of this service's own schema (Contract 14 §8).

-- Schema (own schema only — Contract 14 §8 cross-service rule).
CREATE SCHEMA IF NOT EXISTS example;

-- Least-privilege runtime role (Contract 14 §4). Password injected from Doppler out-of-band.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'example_runtime') THEN
    CREATE ROLE example_runtime NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA example TO example_runtime;

-- Tenant-scoped table (Contract 13 §4 rule 1).
CREATE TABLE example.widgets (
  widget_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID        NOT NULL,
  name        TEXT        NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index that STARTS with tenant_id (Contract 13 §4 rule 1).
CREATE INDEX ix_widgets_tenant ON example.widgets (tenant_id, created_at);

-- Least-privilege DML grants for the runtime role.
GRANT SELECT, INSERT, UPDATE, DELETE ON example.widgets TO example_runtime;

-- Row Level Security (Contract 13 §4 rule 4).
ALTER TABLE example.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE example.widgets FORCE ROW LEVEL SECURITY;

CREATE POLICY p_widgets_tenant ON example.widgets
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
