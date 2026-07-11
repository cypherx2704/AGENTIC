# tool-flow-bridge migrations

Apply in filename order against the Postgres DIRECT endpoint as a superuser / migration
role. Files are idempotent (`IF NOT EXISTS` / `DROP POLICY IF EXISTS`).

| File | Purpose |
|------|---------|
| `20260711_0001__init.sql` | `flow_tools` schema, role `flow_tools_user`, `tenant_runtimes` + `tool_bindings` tables, split RLS, grants. |

In `infra/compose` these are mounted into the `migrate` one-shot (profile `migrate`) at
`/migrations/tool-flow-bridge` and run against the Neon DIRECT endpoint, which also
provisions the `flow_tools_user` role.
