-- =====================================================================================
-- xagent — orchestration (sub-agent workflow) tables. PostgreSQL 16. Idempotent.
-- Apply AFTER 20260705_0007.
--
-- Adds the execution-engine state for the PROMPT -> ORCHESTRATOR -> SUB-AGENTS workflow
-- (SUBAGENT_WORKFLOW_PLAN.md §3.1). The orchestrator (one per tenant, agent_type =
-- 'orchestrator' in auth.agents) decomposes a goal into a DAG and runs each node as one of
-- its OWN sub-agents INTERNALLY (in-tenant, on_behalf_of the sub-agent — no A2A). A2A stays
-- reserved for the external/cross-vendor boundary.
--
--   * xagent.workflows       — one orchestration run (goal, status, DAG, budget, aggregate cost)
--   * xagent.workflow_tasks  — one DAG node (assigned sub-agent, depends_on, spawned task, output)
--   * xagent.agent_presets   — reusable {system prompt, tools, scopes, model} bundles
--                              (the ".claude/agents" analogue) a node can instantiate
--   * xagent.tasks           — + parent_task_id, workflow_id (subtask lineage; NULL for
--                              standalone single-agent tasks — the public /v1/tasks path)
--
-- RLS: tenant-scoped, identical predicate to agents/tasks/task_steps. workflows and
-- workflow_tasks also carry the additive OPT-IN sweeper bypass (app.sweeper = 'on') so the
-- backup sweeper can finalise a run whose driver died — same pattern as tasks/task_steps.
-- agent_presets is not swept. Contracts: workflows/dag.schema.json + run.schema.json.
-- All idempotent (CREATE TABLE / ADD COLUMN / ADD CONSTRAINT IF NOT EXISTS). Safe to re-run.
-- =====================================================================================

-- ── workflows (one orchestration run) ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.workflows (
  workflow_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID         NOT NULL,
  root_agent_id   UUID         NOT NULL,                 -- the orchestrator agent that owns the run
  goal            TEXT         NOT NULL,
  status          VARCHAR(20)  NOT NULL DEFAULT 'pending',
  mode            VARCHAR(20)  NOT NULL DEFAULT 'subagents',
  decomposition   VARCHAR(20),                            -- template | llm (NULL until decomposed)
  subtask_dag     JSONB,                                  -- workflows/dag.schema.json document
  output          JSONB,                                  -- synthesized final result (<=256 KiB)
  error_code      VARCHAR(50),
  error_msg       TEXT,
  tokens_used     INTEGER,
  cost_usd        NUMERIC(12,8),
  cost_budget_usd NUMERIC(12,8),                          -- per-run ceiling; breach -> early-stop cancel
  approval_due_at TIMESTAMPTZ,                            -- for status = awaiting_approval
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  started_at      TIMESTAMPTZ,
  completed_at    TIMESTAMPTZ,
  timeout_at      TIMESTAMPTZ,
  version         INTEGER      NOT NULL DEFAULT 1,        -- optimistic lock
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

-- ── workflow_tasks (one DAG node) ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xagent.workflow_tasks (
  id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id         UUID         NOT NULL REFERENCES xagent.workflows(workflow_id) ON DELETE CASCADE,
  tenant_id           UUID         NOT NULL,
  node_id             TEXT         NOT NULL,              -- DAG node id (unique within a workflow)
  task_id             UUID,                               -- the spawned xagent.tasks id (once running)
  parent_node_id      TEXT,
  description         TEXT         NOT NULL DEFAULT '',
  node_type           VARCHAR(20)  NOT NULL DEFAULT 'agent',
  assigned_agent_id   UUID,                               -- the sub-agent this node runs as
  preset              TEXT,
  depends_on          TEXT[]       NOT NULL DEFAULT '{}', -- upstream node_ids
  status              VARCHAR(20)  NOT NULL DEFAULT 'pending',
  output              JSONB,                              -- summary + citations (NOT the transcript)
  tokens_used         INTEGER,
  cost_usd            NUMERIC(12,8),
  retry_count         INTEGER      NOT NULL DEFAULT 0,
  retry_max           INTEGER      NOT NULL DEFAULT 1,
  approval_request_id UUID,                               -- auth HIL approval_requests.request_id when paused
  created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  started_at          TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  version             INTEGER      NOT NULL DEFAULT 1,     -- optimistic lock (fan-in synthesis nodes)
  CONSTRAINT workflow_task_status_enum CHECK (status IN
    ('pending','running','awaiting_approval','completed','failed','cancelled','timeout','skipped')),
  CONSTRAINT workflow_task_node_type_enum CHECK (node_type IN
    ('task','agent','tool','skill','approval','condition','fanout','join','human')),
  CONSTRAINT uq_workflow_node UNIQUE (workflow_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_tasks_workflow ON xagent.workflow_tasks (workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_tasks_tenant   ON xagent.workflow_tasks (tenant_id);
CREATE INDEX IF NOT EXISTS idx_workflow_tasks_task_id  ON xagent.workflow_tasks (task_id) WHERE task_id IS NOT NULL;

-- ── agent_presets (reusable sub-agent bundles; the ".claude/agents" analogue) ───────────
CREATE TABLE IF NOT EXISTS xagent.agent_presets (
  preset_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID         NOT NULL,
  name           VARCHAR(100) NOT NULL,
  description    TEXT,
  system_prompt  TEXT,
  model_alias    VARCHAR(100),
  allowed_tools  TEXT[]       NOT NULL DEFAULT '{}',
  allowed_scopes TEXT[]       NOT NULL DEFAULT '{}',
  metadata       JSONB        NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_agent_preset_name UNIQUE (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_agent_presets_tenant ON xagent.agent_presets (tenant_id);

-- ── xagent.tasks — subtask lineage columns (NULL for standalone single-agent tasks) ─────
ALTER TABLE xagent.tasks
  ADD COLUMN IF NOT EXISTS parent_task_id UUID,
  ADD COLUMN IF NOT EXISTS workflow_id    UUID;
CREATE INDEX IF NOT EXISTS idx_tasks_workflow_id
  ON xagent.tasks (workflow_id) WHERE workflow_id IS NOT NULL;

-- ── Row Level Security (Contract 13) ───────────────────────────────────────────────────
ALTER TABLE xagent.workflows      ENABLE ROW LEVEL SECURITY;
ALTER TABLE xagent.workflow_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE xagent.agent_presets  ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS xagent_workflows_isolation ON xagent.workflows;
CREATE POLICY xagent_workflows_isolation ON xagent.workflows FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS xagent_workflow_tasks_isolation ON xagent.workflow_tasks;
CREATE POLICY xagent_workflow_tasks_isolation ON xagent.workflow_tasks FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS xagent_agent_presets_isolation ON xagent.agent_presets;
CREATE POLICY xagent_agent_presets_isolation ON xagent.agent_presets FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Additive OPT-IN sweeper bypass (OR-combined) — lets the backup sweeper finalise a run whose
-- driver died. Normal task-path txns never set app.sweeper, so isolation is unchanged for them.
DROP POLICY IF EXISTS xagent_workflows_sweeper ON xagent.workflows;
CREATE POLICY xagent_workflows_sweeper ON xagent.workflows FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

DROP POLICY IF EXISTS xagent_workflow_tasks_sweeper ON xagent.workflow_tasks;
CREATE POLICY xagent_workflow_tasks_sweeper ON xagent.workflow_tasks FOR ALL
  USING      (current_setting('app.sweeper', true) = 'on')
  WITH CHECK (current_setting('app.sweeper', true) = 'on');

-- ── Grants to the runtime role (xagent_user) ───────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON xagent.workflows      TO xagent_user;  -- DELETE: cascade/retention
GRANT SELECT, INSERT, UPDATE, DELETE ON xagent.workflow_tasks TO xagent_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON xagent.agent_presets  TO xagent_user;

-- =====================================================================================
-- end 20260712_0008__orchestration.sql
-- =====================================================================================
