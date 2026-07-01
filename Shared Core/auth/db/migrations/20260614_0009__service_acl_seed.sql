-- =====================================================================================
-- auth-service — service_acl caller-edge seed (Component 8b / Contract 12). PostgreSQL 16.
--
-- WHY THIS EXISTS
-- ---------------
-- POST /v1/service-tokens authenticates a caller by its `X-Service-Name` header (the SHORT
-- service-principal name) and then derives the token scopes from the UNION of `allowed_scopes`
-- across every `auth.service_acl` row whose `caller_service` = that header value
-- (ai.cypherx.auth.service.ServiceTokenService.issue -> ServiceAclRepository.unionAllowedScopes).
-- A caller with NO matching ACL row gets 403 and CANNOT mint a service token — which then makes
-- its downstream `POST /v1/authorize` calls impossible (the caller can't even authenticate to
-- Auth). The first-cycle seed (20260606_0002) only seeded LONG target-style caller names
-- (`llms-gateway`, `guardrails-service`) plus `xagent`, while the real callers present the SHORT
-- principal names configured in `cypherx.service-auth.bootstrap-secrets` / each service's
-- `service_principal_name`: `xagent`, `llms`, `guardrails`, `rag`, `memory`. The rag/memory
-- repos additionally tried to seed these edges with the WRONG columns
-- (`source_service`/`scopes` instead of `caller_service`/`allowed_scopes`), so those inserts no-op.
--
-- This migration is the canonical, ADDITIVE fix: it inserts the correct caller→target edges keyed
-- by the SHORT `caller_service` names against the canonical `caller_service` / `target_service` /
-- `allowed_scopes` columns, so each service can mint its service token and call /v1/authorize.
--
-- It does NOT modify or remove any existing row (the 0002 edges stay as-is); it only ADDS rows.
-- Idempotent: ON CONFLICT (caller_service, target_service) DO NOTHING. Safe to re-run.
--
-- SCOPES: `internal:read` / `internal:write` are the first-cycle internal scopes (mirrors 0002).
-- A caller's effective minted scope set is the UNION across all its target rows.
-- =====================================================================================

INSERT INTO auth.service_acl (caller_service, target_service, allowed_scopes) VALUES
  -- xAgent orchestrates every SharedCore service (X-Service-Name = 'xagent').
  ('xagent',     'auth-service',       ARRAY['internal:read']),
  ('xagent',     'llms-gateway',       ARRAY['internal:read','internal:write']),
  ('xagent',     'guardrails-service', ARRAY['internal:read','internal:write']),
  ('xagent',     'rag-service',        ARRAY['internal:read']),
  ('xagent',     'memory-service',     ARRAY['internal:read','internal:write']),

  -- llms-gateway (X-Service-Name = 'llms') verifies/authorizes against auth.
  ('llms',       'auth-service',       ARRAY['internal:read']),

  -- guardrails-service (X-Service-Name = 'guardrails') verifies/authorizes against auth.
  ('guardrails', 'auth-service',       ARRAY['internal:read']),

  -- rag-service (X-Service-Name = 'rag') calls llms-gateway embeddings + auth (JWKS/authorize).
  ('rag',        'auth-service',       ARRAY['internal:read']),
  ('rag',        'llms-gateway',       ARRAY['internal:read','internal:write']),

  -- memory-service (X-Service-Name = 'memory') calls llms-gateway embeddings + auth.
  ('memory',     'auth-service',       ARRAY['internal:read']),
  ('memory',     'llms-gateway',       ARRAY['internal:read','internal:write'])
ON CONFLICT (caller_service, target_service) DO NOTHING;

-- =====================================================================================
-- end 20260614_0009__service_acl_seed.sql
-- =====================================================================================
