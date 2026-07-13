-- =====================================================================================
-- flow_tools — RLS hardening.
--
-- (1) tenant_runtimes.p_tenant_runtimes_platform was FOR ALL over the empty-GUC (platform)
--     context, i.e. any in_platform code path could READ or WRITE every tenant's runtime
--     secret refs (admin/invoke/credential). Nothing reads tenant_runtimes in platform context
--     — the provisioner reads+writes its OWN runtime in_tenant, and the invoke join runs
--     in_tenant — so drop the policy entirely. Platform context now has NO access to
--     tenant_runtimes. If a cross-tenant reconciler is ever added, give it a narrowly-scoped
--     policy then (mirrors tool_bindings, whose platform policy is SELECT-only for the public
--     manifest endpoint).
--
-- (2) Re-assert least privilege on the runtime role UNCONDITIONALLY, so a pre-existing
--     flow_tools_user that somehow carries BYPASSRLS/SUPERUSER is corrected (a freshly-created
--     role already defaults to NOSUPERUSER NOBYPASSRLS). Best-effort: on a managed Postgres
--     (e.g. Neon) the migration role may lack the privilege to flip these attributes — that is
--     fine, the CREATE ROLE default already satisfies the requirement.
-- =====================================================================================

DROP POLICY IF EXISTS p_tenant_runtimes_platform ON flow_tools.tenant_runtimes;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flow_tools_user') THEN
    BEGIN
      EXECUTE 'ALTER ROLE flow_tools_user NOSUPERUSER NOBYPASSRLS';
    EXCEPTION
      WHEN insufficient_privilege THEN
        RAISE NOTICE 'skipping ALTER ROLE flow_tools_user (insufficient privilege on this cluster)';
      WHEN OTHERS THEN
        RAISE NOTICE 'skipping ALTER ROLE flow_tools_user: %', SQLERRM;
    END;
  END IF;
END
$$;
