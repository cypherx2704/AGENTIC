-- =====================================================================================
-- auth-service — WP04 (Auth completion II). PostgreSQL 16.
--
-- Audit pipeline (export/mirror) + /v1/usage rollup support DDL:
--
--   1. auth.audit_export_jobs   on-demand audit-export job records (who exported what,
--                               where it landed, the presigned URL + expiry) + RLS.
--
-- The /v1/usage rollup TARGET (auth.tenant_usage_counters) already exists from WP03
-- (20260610_0004) — this migration adds NOTHING to it; the WP04 cypherx.llms.usage.recorded
-- consumer UPSERTs into that existing table. The audit_log mirror writes to OBJECT STORAGE
-- (S3/MinIO/local filesystem), not to a DB table, so no DDL is needed for the mirror.
--
-- Idempotent: every object is guarded (IF NOT EXISTS / DO-block), so the file is safe to
-- re-run. New RLS policies/constraints use a DO-block guard — never a bare ALTER ... ADD.
-- =====================================================================================

-- ── 1. auth.audit_export_jobs (Component 6 export — WP04) ─────────────────────────────
-- Records each GET /v1/audit-log/export run: the requesting tenant, the actor, the object
-- key/uri the JSONL landed at, the row count, and the presigned-URL expiry. Tenant-scoped
-- (RLS on app.tenant_id) like the other tenant tables — a tenant:admin only sees its own
-- export history; a platform:admin reads any via the platform read path. The presigned URL
-- itself is NOT stored (it is short-lived and re-derivable) — only its expiry, the key, and
-- the canonical store uri are kept for an operator audit trail.
CREATE TABLE IF NOT EXISTS auth.audit_export_jobs (
  export_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID         NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  requested_by  UUID,                                  -- acting admin agent id (px0/system if absent)
  store_backend VARCHAR(20)  NOT NULL,                 -- local | s3 | minio | noop
  object_key    TEXT         NOT NULL,                 -- store-relative key of the JSONL object
  object_uri    TEXT         NOT NULL,                 -- canonical store uri (s3://… | file://…)
  row_count     BIGINT       NOT NULL DEFAULT 0,
  truncated     BOOLEAN      NOT NULL DEFAULT FALSE,   -- true when exportMaxRows was hit
  window_from   TIMESTAMPTZ,                           -- optional export window lower bound
  window_to     TIMESTAMPTZ,                           -- optional export window upper bound
  url_expires_at TIMESTAMPTZ NOT NULL,                 -- presigned-URL expiry (TTL = 7d default)
  status        VARCHAR(20)  NOT NULL DEFAULT 'completed',  -- completed | failed
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_export_jobs_tenant
  ON auth.audit_export_jobs (tenant_id, created_at DESC);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'auth' AND c.relname = 'audit_export_jobs' AND c.relrowsecurity
  ) THEN
    ALTER TABLE auth.audit_export_jobs ENABLE ROW LEVEL SECURITY;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'auth' AND tablename = 'audit_export_jobs'
      AND policyname = 'p_audit_export_jobs_tenant'
  ) THEN
    CREATE POLICY p_audit_export_jobs_tenant ON auth.audit_export_jobs
      USING (tenant_id = current_setting('app.tenant_id')::uuid);
  END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON auth.audit_export_jobs TO auth_user;

-- =====================================================================================
-- end 20260611_0008__audit_pipeline.sql
-- =====================================================================================
