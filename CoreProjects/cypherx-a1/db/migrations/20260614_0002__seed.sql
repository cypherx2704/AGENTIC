-- =====================================================================================
-- cypherx-a1 — seed.
--
-- (1) auth.service_acl edges so cypherx-a1 may mint Contract-12 service tokens for the
--     SharedCore services it calls. Seeded HERE (not in SharedCore/auth) to keep auth
--     untouched; the compose migrate job applies cypherx-a1 AFTER auth, so auth.service_acl
--     already exists. Guarded on the table existing AND using the CANONICAL columns
--     (caller_service, target_service, allowed_scopes) — NOT the rag-seed's buggy
--     (source_service, scopes). Idempotent via ON CONFLICT.
--
--     allowed_scopes use the platform's internal:read/internal:write convention (same as
--     the xagent edges): the target service maps internal:read -> its read scope (e.g.
--     rag:query / mem:read) and internal:write -> its write scope (rag:ingest / mem:write).
--
-- (2) Connectors, resource ACLs, and RAG KB bindings are created per-tenant at RUNTIME via
--     the API; nothing tenant-scoped is seeded here.
-- =====================================================================================

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
     WHERE table_schema = 'auth' AND table_name = 'service_acl'
  ) THEN
    INSERT INTO auth.service_acl (caller_service, target_service, allowed_scopes) VALUES
      ('cypherx-a1', 'auth-service',       ARRAY['internal:read']),
      ('cypherx-a1', 'llms-gateway',       ARRAY['internal:read','internal:write']),
      ('cypherx-a1', 'guardrails-service', ARRAY['internal:read','internal:write']),
      ('cypherx-a1', 'rag-service',        ARRAY['internal:read','internal:write']),
      ('cypherx-a1', 'memory-service',     ARRAY['internal:read','internal:write'])
    ON CONFLICT (caller_service, target_service) DO NOTHING;
  END IF;
END
$$;
