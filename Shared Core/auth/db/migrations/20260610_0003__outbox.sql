-- =====================================================================================
-- auth-service — transactional outbox (Phase 2 Amendment Log 2026-06 / WP02).
--
-- `auth.outbox` carries every provisioning-critical event so the event row commits in
-- the SAME transaction as the state change it describes (no log-and-drop; ≤5s staleness
-- SLA, Audit Addendum #6). An in-service relay (`OutboxRelay`) polls unpublished rows,
-- publishes them to Kafka, and stamps `published_at`; failures increment `attempts` +
-- `last_error` and are retried forever with a capped backoff.
--
-- Covered topics (written by `OutboxEventWriter`):
--   cypherx.tenant.*                 created | suspended | resumed | plan_changed |
--                                    pending_deletion | deleted   (Contract 13 backbone)
--   cypherx.auth.token.revoked       Component 3c
--   cypherx.auth.policy.changed      /authorize cache invalidation
--   cypherx.auth.config.updated      Component 4 rate-limit/config hot-reload
--
-- Shape mirrors the other services' outboxes (llms.outbox / guardrails.outbox).
-- PLATFORM-scoped: NO RLS — the relay must drain every tenant's rows in one pass, and
-- only Auth itself reads/writes this table. `partition_key` carries the Kafka message
-- key (tenant_id for tenant-scoped events; Contract 5 §4).
--
-- Idempotent: safe to re-run (IF NOT EXISTS guards).
-- =====================================================================================

CREATE TABLE IF NOT EXISTS auth.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,         -- Kafka message key (tenant_id; Contract 5 §4)
  payload       JSONB        NOT NULL,         -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,                   -- NULL until the relay publishes
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
  ON auth.outbox (created_at) WHERE published_at IS NULL;

-- Runtime role drains and stamps rows (platform-scoped table; no RLS).
GRANT SELECT, INSERT, UPDATE, DELETE ON auth.outbox TO auth_user;

-- =====================================================================================
-- end 20260610_0003__outbox.sql
-- =====================================================================================
