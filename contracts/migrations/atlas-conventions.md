# Contract 14 — Schema Migration Standard ⚡

> **Status:** ⚡ First Cycle. Normative. Every service that owns a PostgreSQL schema MUST use the
> same migration **tool** and **conventions** so the platform can reason about schema state
> consistently.

A reference layout that satisfies this contract lives in
[`service-template/`](./service-template/).

---

## 1. Tool — Atlas

**Atlas** (`atlasgo.io`) — declarative **+** versioned migrations, Postgres-native, integrates with
CI.

> **Alternatives considered:** Flyway (Java tooling overhead), Liquibase (XML-heavy),
> Goose / golang-migrate (no declarative mode). **Atlas was chosen for its hybrid
> declarative+versioned model and first-class CI integration.**

---

## 2. Convention — repository layout

```
<service-repo>/db/migrations/
  ├── 20260522_0900__init.sql             ← versioned migration (timestamp + name)
  ├── 20260530_1430__add_capabilities.sql
  └── schema.sql                          ← declarative HCL/SQL snapshot of current state
```

- Versioned migration files are named **`<timestamp>__<name>.sql`** — a sortable timestamp
  (`YYYYMMDD_HHMM`), a double underscore, then a short snake_case name.
- **`schema.sql`** is the **declarative snapshot of the current desired state**. `atlas schema diff`
  compares the migration history against this snapshot to detect drift (see §3).

---

## 3. CI gates

A PR that touches `db/migrations/` **cannot merge** unless all of the following pass:

- **`atlas migrate lint`** — PR cannot merge if it finds **destructive changes** without an
  `# atlas:nolint destructive` comment on the offending statement.
- **`atlas schema diff`** between PR and `main` shows **no unintended drift** (the migration history
  and `schema.sql` agree).
- **All migrations applied in CI integration tests** against a **real PostgreSQL container** (not a
  mock).

---

## 4. Runtime

- Migrations run as a **Kubernetes `Job`** that **completes before** the service `Deployment` becomes
  `Ready`, wired via Helm hooks:
  ```yaml
  "helm.sh/hook": pre-install,pre-upgrade
  ```
- **Two distinct DB users:**
  - The **migration Job uses a privileged DB user (DDL)**.
  - The **service runtime uses the least-privilege per-service user**.
- **Credential paths in Doppler are mandatory** (the Helm chart resolves these by path):

  | Credential | Doppler path |
  |------------|--------------|
  | DDL (migration Job) password | `db/<service>/ddl_password` |
  | Runtime (service) password | `db/<service>/runtime_password` |

  The naming convention is **mandatory** — the Helm chart resolves these by path, so deviating from
  it breaks deployment.

---

## 5. Rollback strategy — expand–contract, roll forward only

**We do NOT run down-migrations in production.** The only safe rollback is to **ship a new corrective
migration**.

| Phase | Release | Rule |
|-------|---------|------|
| **Expand** | release N | Add columns / tables / indexes **only**. Existing code keeps working. New code may write the new shape **AND** the old shape (**dual-write**). |
| **Migrate data** | release N or N+1 | Backfill in a **chunked job**; **never block release**. |
| **Switch** | release N+1 | New code reads the **new shape exclusively**. |
| **Contract** | release N+2 or later | **Drop** deprecated columns / tables — **only after old code is gone from every environment**. |

---

## 6. Forbidden in any single release

- **Dropping a column the deployed code still reads.**
- **Renaming a column** without an expand-then-contract sequence.
- **Changing a column's type in-place** (always **add new column → dual-write → switch → drop old**).
- **Destructive DDL inside the `pre-upgrade` hook** — it cannot be undone if the new deployment then
  fails to start.

---

## 7. Failure handling

- If the migration **Job fails, Helm aborts the upgrade.** The **previous Deployment continues to
  serve traffic.**
- The operator MUST investigate the Job logs and either:
  - **(a)** fix the migration and re-run, **or**
  - **(b)** write a **corrective migration** and ship that instead.
- **Never `helm rollback` past a successful migration.**

---

## 8. Cross-service rule

- A service migration **may only touch its own schema.** **CI rejects migrations that reference other
  schemas.**
- **RLS policies and per-service runtime roles ARE part of the service's own schema** (and are created
  by the **migration Job's DDL user**).
- The **Helm chart MUST grant the migration role `CREATEROLE` on the service's schema only.**

---

## 9. Reference template

[`service-template/`](./service-template/) ships a conforming minimum:

| File | Purpose |
|------|---------|
| [`service-template/atlas.hcl`](./service-template/atlas.hcl) | Atlas project config (env, dev URL, migration dir, lint policy). |
| [`service-template/schema.sql`](./service-template/schema.sql) | Declarative snapshot: one tenant-scoped table + RLS policy + runtime role. |
| [`service-template/migrations/0001__init.sql`](./service-template/migrations/0001__init.sql) | The initial versioned migration matching `schema.sql`. |
| [`service-template/README.md`](./service-template/README.md) | How to copy & wire the template into a service repo. |

---

## 10. Cross-references

- **Contract 13 (Tenant):** every tenant-scoped table needs `tenant_id` + RLS `USING (tenant_id =
  current_setting('app.tenant_id')::uuid)`; new tenant-scoped tables require a cross-tenant denial
  CI test.
- **Contract 19 (Usage/Quotas):** the `auth.tenant_quotas` table and per-service caches are migrated
  under this standard.
