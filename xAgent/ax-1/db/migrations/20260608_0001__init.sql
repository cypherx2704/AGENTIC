-- =====================================================================================
-- xAgent agent-runtime — first-cycle schema (Phase 9A). PostgreSQL 16.
--
-- Run as a superuser / migration role. The `xagent` schema is assumed to already exist
-- (created in Phase 1) but is created idempotently here so the file runs standalone.
-- Creates the first-cycle tables, indexes, Row Level Security (Contract 13), and the
-- grants the runtime role `xagent_user` needs.
--
-- TENANT-SCOPED tables (tenant_id + tenant-leading index + RLS USING app.tenant_id):
--   agents, tasks, task_steps
-- PLATFORM-INTERNAL table (no RLS — internal cross-tenant publish queue):
--   outbox
--
-- The runtime role connects and runs every tenant-scoped query inside
--   BEGIN; SELECT set_config('app.tenant_id', '<uuid>', true); ...; COMMIT
-- (the Core in_tenant() helper). The runtime role is NOT a superuser and does NOT
-- BYPASSRLS, so RLS is enforced. RLS policies use current_setting('app.tenant_id', true)
-- with missing_ok=true so the background outbox publisher (which sets no tenant) is not
-- blocked by a hard error on the RLS'd tables (it only touches the non-RLS outbox).
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS xagent;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xagent_user') THEN
    CREATE ROLE xagent_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA xagent TO xagent_user;

-- =====================================================================================
-- TENANT-SCOPED TABLES
-- =====================================================================================

-- ── agents (Component 1 — runtime config) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.agents (
  agent_id              UUID         PRIMARY KEY,
                        -- same UUID as auth.agents.agent_id; NO cross-schema FK.
  tenant_id             UUID         NOT NULL,
  name                  VARCHAR(255) NOT NULL,
  runtime_version       VARCHAR(50)  NOT NULL DEFAULT '1.0.0',
  status                VARCHAR(20)  NOT NULL DEFAULT 'active',

  -- LLM configuration
  llm_model             VARCHAR(100) NOT NULL DEFAULT 'smart',
  system_prompt         TEXT         NOT NULL,
  max_tokens            INTEGER      NOT NULL DEFAULT 2048,
  temperature           FLOAT        NOT NULL DEFAULT 0.7,

  -- Integration config
  memory_scope          VARCHAR(20)  NOT NULL DEFAULT 'agent',
  guardrail_policy_id   UUID,
  allowed_tools         TEXT[]       NOT NULL DEFAULT '{}',
  allowed_skills        TEXT[]       NOT NULL DEFAULT '{}',
  allowed_kb_ids        UUID[]       NOT NULL DEFAULT '{}',
  rag_top_k_per_kb      INTEGER      NOT NULL DEFAULT 5,
  rag_min_score         FLOAT        NOT NULL DEFAULT 0.7,
  token_budget_per_task INTEGER      NOT NULL DEFAULT 10000,

  -- Capability advertisement (for A2A routing)
  capabilities          JSONB        NOT NULL DEFAULT '[]',
  metadata              JSONB        NOT NULL DEFAULT '{}',

  created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  CONSTRAINT memory_scope_enum CHECK (memory_scope IN ('none','agent','user','tenant','session')),
  CONSTRAINT status_enum       CHECK (status IN ('active','inactive','pending_config')),
  CONSTRAINT temperature_range CHECK (temperature >= 0.0 AND temperature <= 2.0)
);
CREATE INDEX IF NOT EXISTS idx_xagent_agents_tenant ON xagent.agents (tenant_id);

-- ── tasks (Component 2) ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.tasks (
  task_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id      UUID         NOT NULL,
  tenant_id     UUID         NOT NULL,
  user_id       UUID,                       -- opaque tenant-local user id
  trace_id      UUID         NOT NULL,
  status        VARCHAR(20)  NOT NULL DEFAULT 'pending',
  input         JSONB        NOT NULL,
  output        JSONB,
  error_code    VARCHAR(50),
  error_msg     TEXT,
  tokens_used   INTEGER,
  cost_usd      NUMERIC(12,8),
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  started_at    TIMESTAMPTZ,
  completed_at  TIMESTAMPTZ,
  timeout_at    TIMESTAMPTZ,

  CONSTRAINT task_status_enum
    CHECK (status IN ('pending','running','completed','failed','cancelled','timeout'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_id  ON xagent.tasks (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant_id ON xagent.tasks (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON xagent.tasks (status);
-- Supports the 30s timeout sweeper CronJob (orphaned running rows).
CREATE INDEX IF NOT EXISTS idx_tasks_running_timeout
  ON xagent.tasks (timeout_at) WHERE status IN ('pending','running');

-- ── task_steps (Component 6 — audit trail) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.task_steps (
  step_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID         NOT NULL,           -- no FK; app-level link (RLS-friendly)
  tenant_id    UUID         NOT NULL,
  step_type    VARCHAR(30)  NOT NULL,
  step_name    VARCHAR(100) NOT NULL,
  status       VARCHAR(20)  NOT NULL,
  input        JSONB,
  output       JSONB,
  duration_ms  INTEGER,
  tokens_used  INTEGER,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,

  CONSTRAINT step_type_enum
    CHECK (step_type IN ('guardrail_check','memory_retrieve','llm_call','tool_call','memory_write','skill_load')),
  CONSTRAINT step_status_enum
    CHECK (status IN ('running','passed','failed','timeout','redacted'))
);
CREATE INDEX IF NOT EXISTS idx_steps_task_id ON xagent.task_steps (task_id);

-- =====================================================================================
-- PLATFORM-INTERNAL TABLE (no RLS — internal cross-tenant publish queue, Component 3b)
-- =====================================================================================

CREATE TABLE IF NOT EXISTS xagent.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,          -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,          -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
  ON xagent.outbox (created_at) WHERE published_at IS NULL;

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13)
-- Every tenant-scoped query runs inside a tx that does
--   SELECT set_config('app.tenant_id','<uuid>',true).
-- Policies use current_setting('app.tenant_id', true) (missing_ok) so an unset GUC
-- yields NULL (no rows) rather than erroring.
-- =====================================================================================

ALTER TABLE xagent.agents     ENABLE ROW LEVEL SECURITY;
ALTER TABLE xagent.tasks      ENABLE ROW LEVEL SECURITY;
ALTER TABLE xagent.task_steps ENABLE ROW LEVEL SECURITY;
-- outbox is an INTERNAL publish queue drained by a background task across ALL tenants;
-- tenant-RLS would block the drain (the publisher sets no app.tenant_id). Isolation is
-- in the payload, not the row. RLS intentionally NOT enabled on outbox.
ALTER TABLE xagent.outbox     DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS xagent_agents_isolation ON xagent.agents;
CREATE POLICY xagent_agents_isolation ON xagent.agents FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS xagent_tasks_isolation ON xagent.tasks;
CREATE POLICY xagent_tasks_isolation ON xagent.tasks FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS xagent_task_steps_isolation ON xagent.task_steps;
CREATE POLICY xagent_task_steps_isolation ON xagent.task_steps FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- =====================================================================================
-- GRANTS to the runtime role (xagent_user). RLS still applies on top of these.
-- =====================================================================================

-- agents: app inserts (runtime registration) + reads (LOAD / capabilities); updates for
-- future config edits.
GRANT SELECT, INSERT, UPDATE ON xagent.agents TO xagent_user;

-- tasks: app inserts (submit), updates (running -> terminal), reads (status endpoint).
GRANT SELECT, INSERT, UPDATE ON xagent.tasks TO xagent_user;

-- task_steps: app inserts (audit) + reads (A2A response projection).
GRANT SELECT, INSERT ON xagent.task_steps TO xagent_user;

-- outbox: app inserts; publisher reads + updates published_at/attempts.
GRANT SELECT, INSERT, UPDATE ON xagent.outbox TO xagent_user;
