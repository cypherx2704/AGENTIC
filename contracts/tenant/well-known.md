# Contract 13 — Reserved Well-Known Tenant UUIDs ⚡

> **Status:** ⚡ First Cycle. Normative. This is the authoritative registry of reserved
> `tenant_id` values. See [`tenant-model.md`](./tenant-model.md) for the full tenant model.

`tenant_id` is a UUID owned by Auth (Contract 13). A small set of UUIDs is **reserved
platform-wide** and carries special handling that **every** tenant-scoped service must implement
identically. No deployment may reassign these UUIDs to a normal customer tenant.

---

## Registry

| UUID | Name | Availability | Default access | Mutation rule |
|------|------|--------------|----------------|---------------|
| `00000000-0000-0000-0000-000000000001` | **platform tenant** | All environments | read-only-by-default | requires scope `platform:admin` |
| `00000000-0000-0000-0000-0000000000ff` | **integration-test tenant** | CI only | full (test fixtures) | **rejected in prod** |

---

## `00000000-0000-0000-0000-000000000001` — platform tenant

The isolation boundary that owns **platform-level resources**:

- **skills-kb** (platform catalog of skills),
- **platform default policies** (default behavioral / guardrail policies),
- **system service tokens** and other platform-owned configuration.

### Usage rules

1. **Read-only-by-default.** Any caller may *read* platform-tenant rows that are intended to be
   shared (e.g. the public skills catalog), subject to that service's normal authorization.
2. **Mutations require scope `platform:admin`.** A service MUST **reject any write** (insert /
   update / delete) targeting `tenant_id = …0001` unless the authenticated caller carries the
   `platform:admin` scope in its JWT (Contract 1). This is enforced **in addition to** RLS — RLS
   alone is not sufficient because platform-tenant rows are intentionally readable cross-tenant.
3. **Never the fallback / default.** A missing or unresolved `tenant_id` MUST NOT silently fall back
   to the platform tenant. Resolution failure is an authentication error, not a default.
4. **Same UUID in every deployment.** Internal, self-hosted, and white-label deployments all use
   this exact UUID for platform-owned resources, so platform content is portable.

---

## `00000000-0000-0000-0000-0000000000ff` — integration-test tenant

A reserved tenant used **exclusively by CI integration tests** (e.g. the `manual-seed` source,
Contract 13 §2; the cross-tenant denial test, Contract 13 §4 rule 4).

### Usage rules

1. **CI only.** This UUID is provisioned by test fixtures (`auth_tenants_seed.sql`) and used to
   exercise tenant-scoped code paths, including the mandatory **cross-tenant denial** test.
2. **Rejected in prod.** Production services MUST **reject** any request or token resolving to
   `tenant_id = …00ff`. The rejection is gated on the deployment environment (e.g. an env flag /
   profile), so the integration-test tenant can never be addressed against production data.
3. **No production data.** No production row may ever carry this `tenant_id`. CI environments may
   freely create and wipe its data.

---

## Anti-patterns (MUST never happen)

- Using the **platform tenant** as a catch-all default when `tenant_id` cannot be resolved.
- Allowing a **write** to the platform tenant without `platform:admin`.
- Allowing the **integration-test tenant** to resolve in a **production** environment.
- Reassigning either reserved UUID to a customer tenant in any deployment.

---

## Cross-references

- [`tenant-model.md`](./tenant-model.md) — full tenant model, sources, lifecycle, enforcement.
- **Contract 1 (JWT):** `platform:admin` scope; `tenant_id` claim resolution.
- **Contract 13 §4:** RLS, `SET LOCAL app.tenant_id`, cross-tenant denial CI test.
