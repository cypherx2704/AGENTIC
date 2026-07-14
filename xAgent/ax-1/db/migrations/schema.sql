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
--   20260623_0006__init.sql                    (orchestrator hierarchy cols on agents)
--   20260705_0007__agent_tool_loop_toggle.sql  (agents.tool_loop_enabled)
--   20260712_0008__orchestration.sql           (workflows, workflow_tasks, agent_presets,
--                                               tasks.parent_task_id + workflow_id)
--   20260713_0009__subagent_description.sql    (agents.description — the planner's routing signal)
--   20260714_0010__drop_agent_presets.sql      (DROPS the agent_presets table 0008 created — dead;
--                                               routing is the planner's decision, not a preset's)
--   20260714_0011__workflow_use_tools.sql      (workflows.use_tools — the run-level tool switch)
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
  -- Routing description (migration 0009): "when to use this agent", written for the ORCHESTRATOR's
  -- planner, not for the agent. Rendered into the planner's capability catalogue next to
  -- allowed_tools. Empty => the roster falls back to system_prompt.
  description           TEXT         NOT NULL DEFAULT '',
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
  -- orchestrator hierarchy (migration 0006; denormalised mirror of auth.agents)
  agent_type            VARCHAR(20)  NOT NULL DEFAULT 'user_created',
  parent_orchestrator_id UUID,
  immutable_llm         BOOLEAN      NOT NULL DEFAULT false,
  created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT memory_scope_enum CHECK (memory_scope IN ('none','agent','user','tenant','session')),
  CONSTRAINT status_enum       CHECK (status IN ('active','inactive','pending_config')),
  CONSTRAINT temperature_range CHECK (temperature >= 0.0 AND temperature <= 2.0),
  CONSTRAINT xagent_agent_type_enum CHECK (agent_type IN ('orchestrator','sub_agent','user_created'))
);
CREATE INDEX IF NOT EXISTS idx_xagent_agents_tenant ON xagent.agents (tenant_id);
CREATE INDEX IF NOT EXISTS idx_xagent_agents_parent
  ON xagent.agents (parent_orchestrator_id) WHERE parent_orchestrator_id IS NOT NULL;

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
  parent_task_id       UUID,            -- 0008 — subtask lineage (NULL for standalone tasks)
  workflow_id          UUID,            -- 0008 — owning orchestration run (NULL for standalone)
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
CREATE INDEX IF NOT EXISTS idx_tasks_workflow_id
  ON xagent.tasks (workflow_id) WHERE workflow_id IS NOT NULL;  -- 0008

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

-- ── orchestration (migration 0008 — sub-agent workflow engine) ─────────────────────────
CREATE TABLE IF NOT EXISTS xagent.workflows (
  workflow_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID         NOT NULL,
  root_agent_id   UUID         NOT NULL,
  goal            TEXT         NOT NULL,
  status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
  mode            VARCHAR(20)  NOT NULL DEFAULT 'subagents',
  use_tools       BOOLEAN      NOT NULL DEFAULT true,     -- run-level tool switch (0011); ANDed with
                                                          -- agents.tool_loop_enabled. false => every
                                                          -- task in the run is a plain chat: TOOL_LOOP
                                                          -- is skipped and the planner sees no tools.
  decomposition   VARCHAR(20),
  subtask_dag     JSONB,
  output          JSONB,
  error_code      VARCHAR(50),
  error_msg       TEXT,
  tokens_used     INTEGER,
  cost_usd        NUMERIC(12,8),
  cost_budget_usd NUMERIC(12,8),
  approval_due_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  started_at      TIMESTAMPTZ,
  completed_at    TIMESTAMPTZ,
  timeout_at      TIMESTAMPTZ,
  version         INTEGER      NOT NULL DEFAULT 1,
  CONSTRAINT workflow_status_enum CHECK (status IN
    ('pending','planning','running','awaiting_approval','completed','failed','cancelled','timeout')),
  CONSTRAINT workflow_mode_enum CHECK (mode IN ('solo','subagents')),
  CONSTRAINT workflow_decomposition_enum CHECK (decomposition IS NULL OR decomposition IN ('template','llm'))
);
CREATE INDEX IF NOT EXISTS idx_workflows_tenant     ON xagent.workflows (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflows_root_agent ON xagent.workflows (root_agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflows_status     ON xagent.workflows (status);
CREATE INDEX IF NOT EXISTS idx_workflows_running_timeout
  ON xagent.workflows (timeout_at) WHERE status IN ('pending','planning','running','awaiting_approval');

CREATE TABLE IF NOT EXISTS xagent.workflow_tasks (
  id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id         UUID         NOT NULL REFERENCES xagent.workflows(workflow_id) ON DELETE CASCADE,
  tenant_id           UUID         NOT NULL,
  node_id             TEXT         NOT NULL,
  task_id             UUID,
  parent_node_id      TEXT,
  description         TEXT         NOT NULL DEFAULT '',
  node_type           VARCHAR(20)  NOT NULL DEFAULT 'agent',
  assigned_agent_id   UUID,
  preset              TEXT,
  depends_on          TEXT[]       NOT NULL DEFAULT '{}',
  status              VARCHAR(20)  NOT NULL DEFAULT 'pending',
  output              JSONB,
  tokens_used         INTEGER,
  cost_usd            NUMERIC(12,8),
  retry_count         INTEGER      NOT NULL DEFAULT 0,
  retry_max           INTEGER      NOT NULL DEFAULT 1,
  approval_request_id UUID,
  created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  started_at          TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  version             INTEGER      NOT NULL DEFAULT 1,
  CONSTRAINT workflow_task_status_enum CHECK (status IN
    ('pending','running','awaiting_approval','completed','failed','cancelled','timeout','skipped')),
  CONSTRAINT workflow_task_node_type_enum CHECK (node_type IN
    ('task','agent','tool','skill','approval','condition','fanout','join','human')),
  CONSTRAINT uq_workflow_node UNIQUE (workflow_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_tasks_workflow ON xagent.workflow_tasks (workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_tasks_tenant   ON xagent.workflow_tasks (tenant_id);
CREATE INDEX IF NOT EXISTS idx_workflow_tasks_task_id  ON xagent.workflow_tasks (task_id) WHERE task_id IS NOT NULL;

-- NB: xagent.agent_presets was created by 0008 and DROPPED by 0010 — it is deliberately absent from
-- this end-state. It was the ".claude/agents" analogue backing preset-driven routing; routing is now
-- the orchestrator LLM's decision alone, and a node's `preset` is simply the NAME of a sub-agent in
-- xagent.agents. Do not re-add it.

ALTER TABLE xagent.workflows      ENABLE ROW LEVEL SECURITY;
ALTER TABLE xagent.workflow_tasks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS xagent_workflows_isolation ON xagent.workflows;
CREATE POLICY xagent_workflows_isolation ON xagent.workflows FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS xagent_workflow_tasks_isolation ON xagent.workflow_tasks;
CREATE POLICY xagent_workflow_tasks_isolation ON xagent.workflow_tasks FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS xagent_workflows_sweeper ON xagent.workflows;
CREATE POLICY xagent_workflows_sweeper ON xagent.workflows FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

DROP POLICY IF EXISTS xagent_workflow_tasks_sweeper ON xagent.workflow_tasks;
CREATE POLICY xagent_workflow_tasks_sweeper ON xagent.workflow_tasks FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

GRANT SELECT, INSERT, UPDATE, DELETE ON xagent.workflows      TO xagent_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON xagent.workflow_tasks TO xagent_user;
