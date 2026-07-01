# Contract 13 — Tenant Model & ID Resolution ⚡

> **Status:** ⚡ First Cycle. Normative. This document is the single shared definition of
> `tenant_id` for the entire CypherX AI platform.

`tenant_id` appears in every JWT, every DB row, every Kafka event, every log line — but its
precise meaning must be **one shared definition**. This contract pins that definition, the source
registry, the lifecycle events, the enforcement rules, and the anti-patterns.

---

## 1. Definition (deployment-neutral)

- `tenant_id` is a **UUID** that identifies an **isolation boundary** in CypherX AI.
- **`tenant_id` is owned by Auth.** Auth is the single source of truth for who isolation
  boundaries belong to. Other systems (px0, external IdPs, self-serve signups) are *issuers* of
  tenant-provisioning events — **they do not own the lifecycle**.
- For **platform-owned resources** (skills-kb, platform default policies, system service tokens),
  `tenant_id` = the well-known **platform tenant UUID**
  `00000000-0000-0000-0000-000000000001`. Services treat this UUID as **read-only-by-default**
  and **reject mutations unless the caller has scope `platform:admin`**.
- The value is **deployment-neutral**: the shape (a UUID owned by Auth) is fixed; the population of
  tenants is supplied per deployment.

### Reserved well-known tenant UUIDs

Authoritative registry: [`well-known.md`](./well-known.md).

| UUID | Meaning | Rule |
|------|---------|------|
| `00000000-0000-0000-0000-000000000001` | platform tenant | read-only-by-default; mutate requires scope `platform:admin` |
| `00000000-0000-0000-0000-0000000000ff` | integration-test tenant | CI only; **rejected in prod** |

---

## 2. Tenant sources

Every tenant has exactly **one** `source` value, persisted on **`auth.tenants.source`**.

| `source` value | Provisioning trigger | Lifecycle owner |
|----------------|----------------------|-----------------|
| `px0-bridge` | Kafka `px0.org.created` consumed by the px0-bridge service | px0 |
| `external-admin` | `POST /v1/admin/tenants` by a platform admin | CypherX platform-admin |
| `self-serve-signup` | External onboarding endpoint (Contract 20): verified email + accepted terms | Self-served; tenant owns its lifecycle |
| `sso-jit` | First successful JWT verification from a configured upstream IdP with `auto_provision: true` | Upstream IdP (Okta, Azure AD, Auth0, custom OIDC) |
| `manual-seed` | Dev / integration-test only — `auth_tenants_seed.sql` fixture | CI |

### Deployment topology

- **Internal CypherX deployment** uses `px0-bridge` as the canonical lifecycle source.
- **External / self-hosted / white-label deployments** use any combination of `external-admin`,
  `self-serve-signup`, or `sso-jit`.
- The **same code path serves all sources** — px0 is one issuer of N.
- **No service may special-case `source = px0-bridge`.** All sources are **equivalent at the data and
  policy layer**.

---

## 3. Tenant lifecycle events (CypherX-native)

Emitted by **Auth regardless of source**. All services subscribe to these CypherX-native topics — see
§5.

| CypherX event | Triggered by | Effect on every tenant-scoped service |
|---------------|--------------|---------------------------------------|
| `cypherx.tenant.created` | Any source in §2 | Each service's `bootstrap-tenant` consumer seeds default rows (`tenant_config`, `plan`, `quotas` — see Contract 19) |
| `cypherx.tenant.suspended` | px0 `org.suspended` **OR** billing failure **OR** admin action **OR** self-serve cancellation | All agents for the tenant marked `status='suspended'`; Auth rejects new tokens (`TENANT_SUSPENDED`) |
| `cypherx.tenant.plan_changed` | Billing event from any billing adapter (Contract 19 emitter, e.g. px0 / Stripe / Chargebee) | Quota tables refresh per the new plan |
| `cypherx.tenant.deleted` | px0 `org.deleted` **OR** admin action **OR** self-serve close-account **+ 30-day grace** | All services run their bulk-wipe handler against the tenant (GDPR right to erasure) |

### Source adapters & topic isolation

- **External lifecycle event ingestion is encapsulated in source-specific *adapters*** —
  `px0-bridge`, `billing-bridge`, `sso-jit-handler`.
- Adapters **translate** source events into CypherX-native `cypherx.tenant.*` topics.
- **All services subscribe only to `cypherx.tenant.*`** — **never to `px0.*` directly**. This
  decouples downstream services from any specific upstream system.
- The `px0.*` foreign-prefix allow-list (Contract 5) is consumed by **px0-bridge only**.

---

## 4. Enforcement rules (every service MUST follow)

1. **Every persisted table includes `tenant_id UUID NOT NULL`** and an index that **starts with
   `tenant_id`**.
2. **Every query** that reads or writes tenant-scoped data **includes `WHERE tenant_id = $1`**.
3. **`tenant_id` is resolved from the JWT** — **never from a request body field**.
4. **Cross-tenant data access is architecturally impossible** (not just policy):
   - **PostgreSQL:** per-service role + Row Level Security policy
     ```sql
     USING (tenant_id = current_setting('app.tenant_id')::uuid)
     ```
     applied to **every** tenant-scoped table.
   - **Application:** a request-scoped middleware sets `SET LOCAL app.tenant_id = ...` on **every
     transaction**.
   - **PgBouncer MUST run in `transaction` pool mode.**
     - `session` mode breaks `SET LOCAL` (leaks settings across requests).
     - `statement` mode breaks multi-statement transactions outright.
     - This is enforced in **Helm chart defaults** for the shared pooler.
   - **Every tenant-scoped DB access MUST run inside an explicit transaction:**
     ```sql
     BEGIN;
       SET LOCAL app.tenant_id = $1;
       -- <queries>
     COMMIT;
     ```
     ORMs / clients **MUST NOT** issue `SET app.tenant_id` (session-level) — **only `SET LOCAL`**.
   - **CI integration tests MUST include a "cross-tenant denial" case** (a tenant-A connection trying
     to read a tenant-B row → returns **0 rows**). **A PR cannot merge without this test for any new
     tenant-scoped table.**
5. **Logging:** every structured log line emits `tenant_id` (already in Contract 6).
6. **Kafka:** every event envelope carries `tenant_id` (already in Contract 5).

---

## 5. Anti-patterns (MUST never happen)

- A service **accepting `tenant_id` from a request body** and trusting it without a JWT cross-check.
- A query **without a `tenant_id` filter** on a tenant-scoped table.
- A migration that **adds a new tenant-scoped table without `tenant_id` + RLS policy**.

---

## 6. Cross-references

- **Contract 1 (JWT):** `tenant_id` claim is the authoritative source for rule 3.
- **Contract 5 (Kafka envelope):** every event carries `tenant_id`; `px0.*` foreign allow-list.
- **Contract 6 (Logging):** every log line carries `tenant_id`.
- **Contract 14 (Migrations):** RLS policy + per-service runtime role are part of the service schema.
- **Contract 19 (Usage/Quotas):** `bootstrap-tenant` seeds plan + quotas on `cypherx.tenant.created`.
- **Contract 20 (Onboarding):** `self-serve-signup` and `sso-jit` source provisioning.
- [`well-known.md`](./well-known.md): reserved tenant UUID registry.
