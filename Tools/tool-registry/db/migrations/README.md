# tool-registry migrations

Idempotent, ordered SQL migrations for the `tools` schema. Apply with any runner
(Atlas, psql, the platform migrate job). Naming: `YYYYMMDD_NNNN__<slug>.sql`.

| File | Purpose |
|------|---------|
| `20260611_0001__init.sql` | `tools`, `tool_versions`, `tool_capabilities`, `tool_health`; indexes; **corrected split RLS + WITH CHECK** (the marketplace-hole fix) on every table incl. `tool_capabilities`; `tool_user` grants. |
| `20260611_0002__seed.sql` | Platform `tool-web-search` tool + version (Contract-4 manifest) + capability/scope row + health row. |

Apply locally (superuser), e.g.:

```bash
docker exec -i cypherx-postgres psql -U cypherx_admin -d cypherx_platform < 20260611_0001__init.sql
docker exec -i cypherx-postgres psql -U cypherx_admin -d cypherx_platform < 20260611_0002__seed.sql
```

## RLS design — closing the marketplace hole

Each tenant-scoped table has THREE policies instead of one permissive `FOR ALL`:

1. `*_read` (`FOR SELECT`) — `USING (tenant_id = current_tenant OR tenant_id IS NULL)`: a
   tenant reads its own rows **and** platform rows (the discovery UNION).
2. `*_write` (`FOR ALL`) — `USING` and `WITH CHECK` both = `tenant_id = current_tenant`:
   a tenant may only INSERT/UPDATE/DELETE its **own** rows. The `WITH CHECK` half is the
   fix — Postgres re-evaluates the predicate against the **new** row, so an attempt to
   write a row carrying another tenant's `tenant_id` (or `NULL` to forge a platform tool)
   is **rejected**. This is applied to every table **including `tool_capabilities`**.
3. `*_platform` (`FOR ALL`) — gated on an **empty** `app.tenant_id` (the poller/seed
   context) and `tenant_id IS NULL`: the platform seed manages platform rows without a
   tenant GUC, and a tenant request (always non-empty GUC) can never match it.

`tool_health` additionally has a `*_poller` policy so the empty-GUC background sweep may
update health for **all** tools (incl. tenant-owned) — but only from the trusted
empty-GUC context, never from a tenant request.

All predicates use `NULLIF(current_setting('app.tenant_id', true), '')::uuid` so an
empty/unset GUC after a pooled-connection reset never throws on the `''::uuid` cast.
