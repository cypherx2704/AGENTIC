# xAgent agent-runtime — database migrations (Phase 9A, Contract 14)

PostgreSQL 16. Runs as a superuser / migration role. The runtime role
`xagent_user` connects without `BYPASSRLS`, so Row Level Security (Contract 13) is
enforced on every tenant-scoped query.

## Files

| File | Purpose |
|------|---------|
| `20260608_0001__init.sql` | Schema `xagent`, tables (`agents`, `tasks`, `task_steps`, `outbox`), indexes, RLS policies, grants. |
| `20260608_0002__seed.sql` | No-op (agents are created per-tenant at runtime via `POST /v1/agents/{agent_id}/runtime`). Kept for migration-sequence parity. |
| `20260610_0003__tasks_metadata.sql` | WP02 — adds `tasks.metadata JSONB NOT NULL DEFAULT '{}'` (persists `TaskRequest.metadata`; reserved keys rejected at the API layer). Idempotent. |
| `20260611_0004__task_lifecycle_sweeper.sql` | WP08 — backup-sweeper support: an additive, OPT-IN RLS bypass policy on `tasks` / `task_steps` (admits rows only inside a tx that sets `app.sweeper = 'on'`, OR-combined with tenant isolation so normal queries are unchanged), `DELETE` grants + retention indexes (`outbox.published_at`, `task_steps.created_at`) for pruning. Idempotent. |
| `20260611_0005__wp12_enhancement_stages.sql` | WP12 — enhancement-stage support: extends the `task_steps.step_type` CHECK enum with `rag_query`, `tool_loop_limit`, `context_truncated`; adds `tasks.session_id` (nullable session correlator) + a partial index, and `tasks.cost_budget_per_task NUMERIC(12,8)` (nullable per-task USD cost cap). Idempotent. |
| `schema.sql` | Flattened end-state snapshot (init + seed); declarative source-of-truth for `atlas schema apply` / drift detection. |
| `atlas.hcl` | Atlas project config (`local` + `ci` envs). |

## Scope model (Contract 13)

- **Tenant-scoped** (RLS `USING (tenant_id = current_setting('app.tenant_id', true)::uuid)`):
  `agents`, `tasks`, `task_steps`. The app sets the tenant per transaction via
  `SELECT set_config('app.tenant_id', '<uuid>', true)` (the `in_tenant()` helper in
  `db/pool.py`). `current_setting(..., true)` uses `missing_ok` so an unset GUC yields
  NULL (no rows) rather than erroring.
- **Platform-internal, NO RLS**: `outbox`. It is an internal cross-tenant publish queue
  drained by a background task that sets no `app.tenant_id`; tenant-RLS would block the
  drain. Isolation lives in the payload, not the row.

## Tables

- `agents` — runtime config (LLM model, system prompt, budgets, guardrail policy).
  `agent_id` is the same UUID as `auth.agents.agent_id` (no cross-schema FK; validated
  at the app layer against Auth `GET /v1/agents/{id}` at registration time). CHECK enums
  on `memory_scope` (`none|agent|user|tenant|session`), `status`
  (`active|inactive|pending_config`), and `temperature` (`[0.0, 2.0]`).
- `tasks` — submitted tasks. CHECK enum on `status`
  (`pending|running|completed|failed|cancelled|timeout`). `error_msg` is the COLUMN name;
  the Kafka `cypherx.agent.task.failed` payload uses `error_message` (Contract 5).
  `metadata` (WP02) carries the caller's free-form request tags (reserved identity keys
  rejected at the API layer).
- `task_steps` — per-stage audit trail. EXACTLY 3 rows per first-cycle task
  (`guardrail_check_input`, `llm_call`, `guardrail_check_output`). Internal `status` enum
  includes `redacted`; the A2A response maps `redacted -> passed`.
- `outbox` — transactional outbox (Component 3b). `partition_key = tenant_id`; the
  publisher loop drains it to Kafka, DLQ after 10 attempts.

## Usage

```sh
# Apply versioned migrations
atlas migrate apply --env local

# Apply / diff against the declarative end-state
atlas schema apply  --env local --to file://schema.sql
atlas migrate diff  --env local
```

`DATABASE_URL` (env) overrides the default local connection string in `atlas.hcl`.
