-- =====================================================================================
-- xAgent agent-runtime — flattened end-state snapshot (init + seed). PostgreSQL 16.
--
-- This is the declarative source-of-truth for `atlas schema apply` / drift detection.
-- It is the concatenation of:
--   20260608_0001__init.sql                  (schema, tables, indexes, RLS, grants)
--   20260608_0002__seed.sql                  (no-op — agents created per-tenant at runtime)
--   20260610_0003__tasks_metadata.sql        (WP02 — tasks.metadata JSONB column)
--   20260611_0004__task_lifecycle_sweeper.sql (WP08 — sweeper RLS bypass + retention)
--   20260611_0005__wp12_enhancement_stages.sql (WP12 — step_type enum + session_id +
--                                               cost_budget_per_task)
-- Keep this file in sync when adding a versioned migration.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS xagent;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xagent_user') THEN
    CREATE ROLE xagent_user LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA xagent TO xagent_user;

-- ── agents (Component 1 — runtime config) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.agents (
  agent_id              UUID         PRIMARY KEY,
  tenant_id             UUID         NOT NULL,
  name                  VARCHAR(255) NOT NULL,
  runtime_version       VARCHAR(50)  NOT NULL DEFAULT '1.0.0',
  status                VARCHAR(20)  NOT NULL DEFAULT 'active',
  llm_model             VARCHAR(100) NOT NULL DEFAULT 'smart',
  system_prompt         TEXT         NOT NULL,
  max_tokens            INTEGER      NOT NULL DEFAULT 2048,
  temperature           FLOAT        NOT NULL DEFAULT 0.7,
  memory_scope          VARCHAR(20)  NOT NULL DEFAULT 'agent',
  guardrail_policy_id   UUID,
  allowed_tools         TEXT[]       NOT NULL DEFAULT '{}',
  -- Per-agent tool-loop toggle (migration 0007): true = full LLM<->tool loop ("multiple
  -- request"); false = TOOL_LOOP stage skips -> a single LLM call ("per request", for
  -- rate-limited / free-tier models). Default true preserves prior behaviour.
  tool_loop_enabled     BOOLEAN      NOT NULL DEFAULT true,
  allowed_skills        TEXT[]       NOT NULL DEFAULT '{}',
  allowed_kb_ids        UUID[]       NOT NULL DEFAULT '{}',
  rag_top_k_per_kb      INTEGER      NOT NULL DEFAULT 5,
  rag_min_score         FLOAT        NOT NULL DEFAULT 0.7,
  token_budget_per_task INTEGER      NOT NULL DEFAULT 10000,
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
  user_id       UUID,
  trace_id      UUID         NOT NULL,
  status        VARCHAR(20)  NOT NULL DEFAULT 'pending',
  input         JSONB        NOT NULL,
  metadata      JSONB        NOT NULL DEFAULT '{}',
  session_id           VARCHAR(255),    -- WP12 — optional session correlator (not identity)
  cost_budget_per_task NUMERIC(12,8),   -- WP12 — optional per-task USD cost cap (NULL = none)
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
CREATE INDEX IF NOT EXISTS idx_tasks_running_timeout
  ON xagent.tasks (timeout_at) WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_tasks_session_id
  ON xagent.tasks (session_id) WHERE session_id IS NOT NULL;  -- WP12

-- ── task_steps (Component 6 — audit trail) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.task_steps (
  step_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID         NOT NULL,
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
    CHECK (step_type IN ('guardrail_check','memory_retrieve','llm_call','tool_call','memory_write',
                         'skill_load','rag_query','tool_loop_limit','context_truncated')),  -- WP12 +3
  CONSTRAINT step_status_enum
    CHECK (status IN ('running','passed','failed','timeout','redacted'))
);
CREATE INDEX IF NOT EXISTS idx_steps_task_id ON xagent.task_steps (task_id);

-- ── outbox (Component 3b — internal publish queue, no RLS) ─────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,
  payload       JSONB        NOT NULL,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
  ON xagent.outbox (created_at) WHERE published_at IS NULL;

-- ── Row Level Security (Contract 13) ───────────────────────────────────────────────────
ALTER TABLE xagent.agents     ENABLE ROW LEVEL SECURITY;
ALTER TABLE xagent.tasks      ENABLE ROW LEVEL SECURITY;
ALTER TABLE xagent.task_steps ENABLE ROW LEVEL SECURITY;
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

-- ── WP08 sweeper RLS bypass (additive PERMISSIVE; OR-combined with isolation) ───────────
-- Admits rows ONLY inside a tx that sets app.sweeper = 'on' (the backup sweeper's
-- cross-tenant discovery + task_steps retention). Normal task-path txns never set it, so
-- tenant isolation is unchanged for them.
DROP POLICY IF EXISTS xagent_tasks_sweeper ON xagent.tasks;
CREATE POLICY xagent_tasks_sweeper ON xagent.tasks FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

DROP POLICY IF EXISTS xagent_task_steps_sweeper ON xagent.task_steps;
CREATE POLICY xagent_task_steps_sweeper ON xagent.task_steps FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

-- ── Grants to the runtime role (xagent_user) ───────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE         ON xagent.agents     TO xagent_user;
GRANT SELECT, INSERT, UPDATE         ON xagent.tasks      TO xagent_user;
GRANT SELECT, INSERT, DELETE         ON xagent.task_steps TO xagent_user;  -- DELETE: WP08 retention
GRANT SELECT, INSERT, UPDATE, DELETE ON xagent.outbox     TO xagent_user;  -- DELETE: WP08 retention

-- ── WP08 retention scan support ─────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_outbox_published_at
  ON xagent.outbox (published_at) WHERE published_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_task_steps_created_at
  ON xagent.task_steps (created_at);
