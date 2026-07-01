# Contract 18 — API Key & Resource ACL Pattern

> **Status:** ⚡ First cycle. **Normative reference.**
> Agent JWTs are *callers* (an agent doing work). External developers, partner integrations, and
> BYO-runtime clients need a separate concept: a **long-lived API key** scoped to *resources*. This
> contract pins one shared pattern every SharedCore service implements.

**Why a contract, not a service-local design:** if each service invents its own API-key model,
SDKs, dashboards, billing, and revocation pipelines fragment. The shape is pinned once here. The
machine-readable ACL row shape is [`api-key-acl.schema.json`](./api-key-acl.schema.json).

---

## 1. Key format

```
cx_<env>_<service>_<random_36_chars>
```

Example: `cx_prod_rag_q9F4j2hN8KpL5xR3vB7tY1cM6sZ0aE2dW9G`

| Segment | Rule |
|---------|------|
| `cx` | Fixed literal prefix identifying a CypherX API key. |
| `<env>` | Deployment environment (e.g. `prod`, `sandbox`). The onboarding sandbox issues `cx_sandbox_...` keys (Contract 20). |
| `<service>` | Issuing service segment (e.g. `rag`, `llms`, `auth`). Lets dashboards/logs identify the issuing service at-a-glance and lets Kong route per-service-key validation. |
| `<random_36_chars>` | Base62 random portion from a CSPRNG. 36 chars → **≥ 213 bits entropy**. |

---

## 2. Per-service tables

Each SharedCore service creates these two tables **in its own schema** (`<service>` = `auth`,
`llms`, `guardrails`, `rag`, `memory`, `tools`, `skills`, `xagent`, …).

### 2.1 `<service>.api_keys`

```sql
CREATE TABLE <service>.api_keys (
  api_key_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL,
  key_prefix        TEXT NOT NULL,              -- first 8 chars of the key, shown in UI for identification
  key_hash          TEXT NOT NULL,              -- Argon2id of the full key; full key shown ONCE at creation
  name              TEXT NOT NULL,              -- human label
  created_by        UUID NOT NULL,              -- px0 user_id OR upstream IdP sub
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at        TIMESTAMPTZ,                -- NULL = no expiry; recommended ≤ 365 days
  last_used_at      TIMESTAMPTZ,
  status            TEXT NOT NULL DEFAULT 'active'  -- active | rotating | revoked
                    CHECK (status IN ('active', 'rotating', 'revoked')),
  default_scopes    TEXT[] NOT NULL DEFAULT '{}',
  metadata          JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_api_keys_tenant ON <service>.api_keys (tenant_id);
CREATE UNIQUE INDEX ix_api_keys_hash ON <service>.api_keys (key_hash);
ALTER TABLE <service>.api_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_api_keys_tenant ON <service>.api_keys
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

**Column rules:**

| Column | Rule |
|--------|------|
| `api_key_id` | UUID primary key, server-generated. |
| `tenant_id` | NOT NULL. RLS-scoped (see policy). |
| `key_prefix` | First **8 chars** of the key, shown in UI for identification. |
| `key_hash` | **Argon2id** of the full key. The full key is shown **ONCE** at creation; the platform cannot recover it. Unique index `ix_api_keys_hash`. |
| `name` | Human label. NOT NULL. |
| `created_by` | px0 `user_id` OR upstream IdP `sub`. NOT NULL. |
| `created_at` | NOT NULL, default `NOW()`. |
| `expires_at` | NULL = no expiry; **recommended ≤ 365 days**. |
| `last_used_at` | Updated on use (drives stale-key reporting). |
| `status` | `active` \| `rotating` \| `revoked`. CHECK constraint enforced. Default `active`. |
| `default_scopes` | `TEXT[]`, default `{}`. Used as the upper bound at exchange time. |
| `metadata` | `JSONB`, default `{}`. |

### 2.2 `<service>.api_key_acls`

```sql
CREATE TABLE <service>.api_key_acls (
  api_key_id      UUID NOT NULL REFERENCES <service>.api_keys(api_key_id) ON DELETE CASCADE,
  tenant_id       UUID NOT NULL,
  resource_type   TEXT NOT NULL,                -- service-specific: 'kb', 'model', 'policy', 'agent', 'tool', 'skill', 'memory_scope', ...
  resource_id     TEXT NOT NULL,                -- '*' = all resources of that type within tenant
  permissions     TEXT[] NOT NULL,              -- service-specific verbs: 'read', 'write', 'invoke', 'admin'
  PRIMARY KEY (api_key_id, resource_type, resource_id)
);
ALTER TABLE <service>.api_key_acls ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_api_key_acls_tenant ON <service>.api_key_acls
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

**Rules:**

- Primary key is `(api_key_id, resource_type, resource_id)`.
- `api_key_id` references `<service>.api_keys(api_key_id)` **ON DELETE CASCADE**.
- `resource_id = '*'` matches **all resources of that type within the tenant**.
- `resource_type` and `permissions` values are **service-specific** — see §6.
- RLS policy `p_api_key_acls_tenant` scopes every row to `app.tenant_id`.
- Machine-readable row shape: [`api-key-acl.schema.json`](./api-key-acl.schema.json).

---

## 3. Lifecycle endpoints

| Operation | Endpoint | Behaviour |
|-----------|----------|-----------|
| **Create** | `POST /v1/api-keys` | Body: `{ name, expires_in_days?, default_scopes[], acls[{resource_type, resource_id, permissions[]}] }`. Returns the **only** copy of the full key + the `api_key_id`. Client MUST persist; the platform cannot recover it. |
| **List** | `GET /v1/api-keys` | Returns `(api_key_id, key_prefix, name, created_at, last_used_at, status)` — **never** the secret. |
| **Rotate** | `POST /v1/api-keys/{id}/rotate` | **Atomic.** Issues a new key, marks the old key `status='rotating'` (still accepted), returns the new key. Old key is accepted for `rotation_grace_seconds` (default **86400 = 24h**), then auto-revoked by a background job. Both keys carry the **same ACLs**. |
| **Revoke** | `DELETE /v1/api-keys/{id}` | Sets `status='revoked'`. Verifier MUST check status on **every** request (cached ≤ 60s). |
| **ACL update** | `PUT /v1/api-keys/{id}/acls` | Replaces the **whole ACL list atomically**. |

---

## 4. Exchange for JWT

Every service does the same dance:

1. Kong receives `Authorization: Bearer cx_<env>_<service>_...`.
2. Kong calls Auth `POST /v1/api-keys/exchange { api_key }` server-to-server, presenting **Kong's
   own service token** (Contract 12).
3. Auth returns a short-lived (**≤ 1h**) JWT carrying:
   - `tenant_id`,
   - `api_key_id`,
   - `scopes = default_scopes ∩ requested_scopes` (intersection),
   - `acls` (compact form) packed into a per-route claim.
4. The exchange result is cached in Auth's Valkey under `api_key_exch:{hash}` with **TTL 5m**.

The exchanged JWT then flows through normal Contract 1 verification.

---

## 5. Enforcement (every route in every service)

1. **JWT verified** (Contract 1).
2. **ACL check:** if the `api_key_id` claim is present, the route's authorization middleware joins
   on `<service>.api_key_acls` to confirm a row matching
   `(resource_type, resource_id, required_permission)` exists. Wildcard `*` for `resource_id`
   matches anything in the tenant.
3. **Audit:** an audit log row is written (Contract 6 log line) **and** a
   `cypherx.<service>.api_key.used` event is emitted — **sampled at 1% in steady state, 100% on a
   permission failure**.

---

## 6. Cross-service ACL summary (each service's resource types)

| Service | `resource_type` values | `permissions` values |
|---------|------------------------|----------------------|
| Auth | `tenant`, `agent`, `policy` | `read`, `write`, `admin` |
| LLMs | `model`, `alias`, `budget`, `provider_key` | `invoke`, `read`, `write` |
| Guardrails | `policy`, `rule`, `violation` | `read`, `write`, `simulate` |
| RAG | `kb`, `document`, `webhook` | `read`, `write`, `ingest`, `query`, `admin` |
| Memory | `scope`, `principal`, `memory_type` | `read`, `write`, `forget` |
| Tools | `tool`, `tool_version` | `invoke`, `read`, `publish`, `deprecate` |
| Skills | `skill`, `skill_kb` | `read`, `submit`, `publish`, `deprecate` |
| xAgent | `agent`, `task`, `workflow` | `read`, `submit`, `cancel` |

> Phase docs for each service MUST enumerate their resource types and permission verbs explicitly
> in their LLD; CI lints that ACL writes in code only use the declared verbs.
