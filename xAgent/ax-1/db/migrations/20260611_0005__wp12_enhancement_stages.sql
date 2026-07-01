-- =====================================================================================
-- xAgent agent-runtime — WP12: enhancement-stage support (RAG / Memory / Tools). PG 16.
--
-- Adds the schema the new enhancement pipeline stages need:
--
--  1. task_steps.step_type CHECK enum EXTENSION. The first-cycle constraint admitted
--     {guardrail_check, memory_retrieve, llm_call, tool_call, memory_write, skill_load}.
--     WP12 adds three new audit step types written by the new stages / PROMPT_BUILD:
--       * rag_query         — the RAG_QUERY stage's per-task retrieval summary;
--       * tool_loop_limit   — the TOOL_LOOP stage hit its max-iterations bound;
--       * context_truncated — PROMPT_BUILD dropped spliced context to fit the budget.
--     (memory_retrieve / tool_call / memory_write / skill_load were already admitted by
--     20260608_0001 — only the three NEW values are added here.) Implemented as a
--     DROP + re-ADD of the named CHECK so the constraint name + full vocabulary stay
--     authoritative and in sync with models/task.py STEP_TYPES.
--
--  2. tasks.session_id (nullable) — an OPTIONAL conversational-session correlator the
--     memory stages use to scope session-scoped memory. NOT identity (tenant/agent come
--     from the JWT); it is a client-supplied request parameter persisted for provenance +
--     memory scope. Nullable: a task with no session carries NULL.
--
--  3. tasks.cost_budget_per_task (nullable) — an OPTIONAL per-task USD cost budget. When
--     set, the LLM + tool-loop stages accrue cost against it and short-circuit
--     BUDGET_EXCEEDED before exceeding it. NULL means no cost cap (the token budget still
--     bounds the LLM call). NUMERIC(12,8) mirrors tasks.cost_usd's precision.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS). Safe to re-run. No
-- new RLS policies or grants: the existing tasks / task_steps isolation policies + grants
-- cover the new columns + step types.
-- =====================================================================================

-- ── 1) task_steps.step_type enum extension (drop + re-add the named CHECK) ─────────────
ALTER TABLE xagent.task_steps DROP CONSTRAINT IF EXISTS step_type_enum;
ALTER TABLE xagent.task_steps
  ADD CONSTRAINT step_type_enum CHECK (step_type IN (
    'guardrail_check',
    'memory_retrieve',
    'llm_call',
    'tool_call',
    'memory_write',
    'skill_load',
    'rag_query',          -- WP12
    'tool_loop_limit',    -- WP12
    'context_truncated'   -- WP12
  ));

-- ── 2) tasks.session_id (nullable session correlator) ─────────────────────────────────
ALTER TABLE xagent.tasks
  ADD COLUMN IF NOT EXISTS session_id VARCHAR(255);

-- A partial index supports session-scoped lookups / future session feeds (non-NULL only).
CREATE INDEX IF NOT EXISTS idx_tasks_session_id
  ON xagent.tasks (session_id) WHERE session_id IS NOT NULL;

-- ── 3) tasks.cost_budget_per_task (nullable per-task USD cost cap) ─────────────────────
ALTER TABLE xagent.tasks
  ADD COLUMN IF NOT EXISTS cost_budget_per_task NUMERIC(12,8);
