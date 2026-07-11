-- =====================================================================================
-- tool-flow-bridge — seed.
--
-- auth.service_acl edges so tool-flow-bridge may mint Contract-12 service tokens for the
-- services it calls (auth for JWKS/mint, tool-registry for tool registration). Seeded HERE
-- (not in SharedCore/auth) to keep auth untouched; the compose migrate job applies
-- tool-flow-bridge AFTER auth, so auth.service_acl already exists. Guarded on the table
-- existing AND using the CANONICAL columns (caller_service, target_service, allowed_scopes).
-- Idempotent via ON CONFLICT. Auth refuses to mint a service token for a caller with no ACL
-- entry ("has no service_acl entry and may not mint a service token").
--
-- allowed_scopes use the platform's internal:read/internal:write convention (same as the
-- xagent / cypherx-a1 edges). The registry takes tenant_id + tool:admin from the FORWARDED
-- user JWT (Contract 13), not from this service token — these scopes only gate the mint.
-- =====================================================================================

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
     WHERE table_schema = 'auth' AND table_name = 'service_acl'
  ) THEN
    INSERT INTO auth.service_acl (caller_service, target_service, allowed_scopes) VALUES
      ('tool-flow-bridge', 'auth-service',  ARRAY['internal:read']),
      ('tool-flow-bridge', 'tool-registry', ARRAY['internal:read','internal:write'])
    ON CONFLICT (caller_service, target_service) DO NOTHING;
  END IF;
END
$$;
