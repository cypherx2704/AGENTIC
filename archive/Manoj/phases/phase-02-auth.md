# Phase 2 — SharedCore / Auth
> **Status:** ⏳ Pending | **Depends On:** Phase 0, Phase 1 | **Blocks:** Phase 3, 4, 5, 6, 7, 9
> **First Cycle:** ⚡ Partial — agent registration, credential issuance, and JWT validation required

## Amendment Log (2026-06 — pre-build reconciliation)

- **jti replay rescope (Component 3b + ⚡ checklist):** single-use `jti` semantics rescoped to one-time credentials only (future DPoP proofs, Component 10 approval tokens, optional service-token nonces). The "every service rejects re-presented jti" item was deleted from the ⚡ checklist — implemented literally it breaks every multi-use 1-hour bearer JWT after its first use. Bearer kill-switch needs are covered by Component 3c jti revocation, which IS enforced verifier-side (WP03).
- **Onboarding split (Component 1c):** first cycle = signup/verify/resend with a config-driven SMTP emitter (MailHog/mock locally), pluggable captcha (mock provider), and velocity-only risk scoring. On verify: create tenant + issue the tenant's first agent + API key. Deleted: `auth.tenant_users` (contradicted the agent-only model), the 7-day session JWT (broke the 1h TTL cap), and the synthetic `upstream_identity` insert (`signup_attempts.tenant_id` already links). `/upgrade` + `/close-account` moved to Phase 11; reputation feeds + manual-review UI moved to Phase 13.
- **Component 11 config-gated:** `auth.upstream_identity` table ships EMPTY — an empty table means px0 verification is disabled (current code behavior is correct). The px0 seed row moves to Phase 11 (px0 is not provisionable in the compose runtime).
- **`GET /v1/usage` data source (Component 1d, WP04):** now backed by a `cypherx.llms.usage.recorded` consumer rolling into `auth.tenant_usage_counters` (DDL added). No cross-schema reads into `llms.*`.
- **Missing DDL resolved:** `auth.rate_limit_config` gets DDL + platform-default seed (Component 4); `auth.tenant_step_up_policy` defers with Component 10 (no first-cycle migration); `auth.tenant_users` deleted per the 1c fix.
- **Component 5c authoritative staging:** Phase 2 = table + ONE shadow seed row only (no middleware); Phase 10 = alert-only middleware; Phase 13 = blocking/quarantine. The four previously contradictory scope statements (component stance, both checklists, Audit Addendum #7) are aligned to this.
- **Auth transactional outbox (Kafka events section):** `auth.outbox` + relay added — same pattern as the other three services — for `cypherx.tenant.*` lifecycle, `cypherx.auth.token.revoked`, and `cypherx.auth.policy.changed`. Best-effort log-and-drop publishing of the cross-service provisioning backbone violated the ≤5s staleness SLA (Audit Addendum #6).
- **Webhooks homed in auth (Component 1e):** moved from the foreign `platform.*` schema to `auth.webhook_subscriptions` + `auth.webhook_deliveries` (DDL added), delivered by an auth-owned worker running as its own compose service. Contract 21 is referenced ONLY for the signing scheme and retry schedule.
- **`tenants.plan` interim ownership (Component 1b):** Auth owns `tenants.plan` (default `'free'`), surfaced as a JWT claim AND via the quotas/limits endpoints; the LLMs gateway caches it 60s in Valkey. Ownership migrates to platform-mgmt later (documented inline).
- **Minor batch:** OIDC discovery no longer advertises the unshipped introspect/revoke endpoints; external service-client token TTL ≤3600s documented as distinct from the 300s internal service-token cap; emergency signing-key rotation interim gate = `platform:admin` + mounted emergency-token file (Component 10 step-up replaces it later); token mint documented as a platform-scoped `key_hash` lookup with `X-Tenant-ID` optional; ⚡ checklist `service_acl` edges aligned to Component 8b's 5-edge seed table.
- **Checklist hygiene (compose-parity):** ⚡ checklist items that demanded deploy-target mechanisms (AWS KMS/IRSA, Kong, Istio, K8s/ArgoCD, Doppler, S3 Object Lock) are restated as compose-parity equivalents buildable in the actual runtime (compose + Neon + Valkey + Redpanda + MinIO), with the originals noted as the cloud forms for the infra phase.
- **Tenant lifecycle subscriber overclaim softened (Component 1b):** "every SharedCore service subscribes to `cypherx.tenant.*`" replaced with the actual first-cycle subscribers — **LLMs and Guardrails** via their bootstrap-tenant consumers; **RAG has NO consumer** (write-through provisioning on first touch, per the Phase 5 amendment). Later services adopt one of the two patterns; the no-direct-`px0.*` rule and the outbox publication guarantee are unchanged. Same fix applied in phase-00 Contracts 13/20.

---

## Phase Overview

SharedCore/Auth is the **agent identity and access management layer** of the platform. It is the second thing built (after infra) because nothing in the platform works without it. Every service validates JWTs against this service. Every agent must have an identity before it can do anything.

This is **not** user authentication — that is px0's responsibility. This service authenticates **agents**: it gives agents an identity, issues them credentials, and makes authorisation decisions when they try to access platform resources.

**Deliverable:** A running auth service that can register agents, issue API keys and JWTs, and answer authorisation queries. All other services integrate with this service's `/authorize` endpoint before performing any action.

> 🏗️ **Service Architecture Note:** The internal architecture of the auth service (language choice, framework, internal module structure, database query patterns, caching strategy) must be planned separately before implementation begins. This phase defines only platform-level contracts and integration points.

---

## High Level Design

### System Context

```
                           ┌────────────────────────────────────┐
                           │         AUTH SERVICE               │
                           │                                    │
  Agent Developer  ───────►│  /agents  (register, manage)       │
                           │  /keys    (issue, rotate, revoke)  │
  Every Service  ──────────│  /token   (issue short-lived JWT)  │◄──── Phase 3, 4, 5, 6, 7, 9
  (validate JWT)           │  /authorize (allow/deny decision)  │
                           │  /policies (RBAC/ABAC rules)       │
                           │                                    │
                           └────────────────┬───────────────────┘
                                            │
                     ┌──────────────────────┼──────────────────────┐
                     ▼                      ▼                      ▼
               PostgreSQL             Valkey Cache           Kafka Events
              (auth schema)       (JWT blacklist,          (agent.registered,
           (agents, keys,         agent-cap cache)          credential.rotated)
            policies, audit)
```

### Where Auth Sits in Request Flow

```
External Request
  → Kong Gateway       (validates JWT signature using Auth's public key)
  → Target Service     (calls Auth /authorize to check scope + tenant)
  → Auth Service       (answers: allowed / denied + reason)
```

### Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| JWT algorithm | RS256 (RSA + SHA-256) | Asymmetric — services only need public key to verify; private key never leaves Auth |
| Key format | JWKS endpoint | Standard; allows automatic key rotation without downtime |
| API key format | `cx_<env>_<random-32-bytes-base64url>` | Identifiable prefix, URL-safe, unguessable |
| Policy storage | PostgreSQL (versioned rows) | Queryable, auditable, survives restarts |
| Auth decision caching | Valkey, 30s TTL | Reduce DB load; short TTL ensures policy changes take effect quickly |

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first to unblock all other phases. 📋 items design now, implement after first cycle.

---

### Component 1 — Agent Registry ⚡

> **Bootstrap super-admin path:**
> The very first `POST /v1/agents` has no caller agent and therefore no JWT scope. Resolved as follows:
> 1. At startup, if `auth.agents` is empty (i.e., fresh install), Auth accepts ONE request bearing header `X-Bootstrap-Token: <doppler:bootstrap/super_admin_token>` against `POST /v1/admin/bootstrap`.
> 2. That request creates the first agent with `platform:admin` scope and inserts a sentinel row in `auth.bootstrap_state` marking bootstrap as complete.
> 3. After the sentinel row exists, the `X-Bootstrap-Token` header is permanently rejected (returns 410 Gone). All subsequent admin operations use normal JWT auth with `platform:admin` scope.
> 4. The bootstrap token in Doppler MAY be rotated post-bootstrap; it has no live use.


**What it is:** The persistent record of every agent registered on the platform.

**Data Model (PostgreSQL — `auth.agents`):**
```sql
CREATE TABLE auth.agents (
  agent_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL,
  name             VARCHAR(255) NOT NULL,
  description      TEXT,
  version          VARCHAR(50) NOT NULL DEFAULT '1.0.0',
  status           VARCHAR(20) NOT NULL DEFAULT 'active',
      -- active | inactive | suspended
  capabilities     JSONB NOT NULL DEFAULT '[]',
      -- declared list of what this agent can do (for A2A routing)
  allowed_scopes   TEXT[] NOT NULL DEFAULT '{}',
      -- scopes this agent is allowed to request tokens for
  allowed_tools    TEXT[] NOT NULL DEFAULT '{}',
  allowed_skills   TEXT[] NOT NULL DEFAULT '{}',
  metadata         JSONB NOT NULL DEFAULT '{}',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by       UUID NOT NULL,     -- user_id from px0

  CONSTRAINT agents_tenant_name_version_unique UNIQUE (tenant_id, name, version)
);

CREATE INDEX idx_agents_tenant_id ON auth.agents(tenant_id);
CREATE INDEX idx_agents_status    ON auth.agents(status);
```

**API Endpoints:**
```
POST   /v1/agents              Register new agent       [scope: platform:admin or tenant:admin]
GET    /v1/agents/{agent_id}   Get agent details        [scope: agent:read]
PATCH  /v1/agents/{agent_id}   Update agent metadata    [scope: platform:admin or tenant:admin]
DELETE /v1/agents/{agent_id}   Deactivate agent         [scope: platform:admin or tenant:admin]
GET    /v1/agents              List agents (paginated)  [scope: tenant:admin]
POST   /v1/admin/bootstrap     ONE-TIME super-admin init [auth: X-Bootstrap-Token header]
```

---

### Component 1b — Tenant Admin ⚡ (NEW)

> Phase 0 Contract 13 says tenants are seeded manually via Auth admin API for first cycle. These endpoints exist for that purpose. The px0 bridge (Phase 11) will eventually drive these via Kafka events; until then, this is the only path to create tenants.

**Data Model (PostgreSQL — `auth.tenants`):**
```sql
CREATE TABLE auth.tenants (
  tenant_id     UUID PRIMARY KEY,           -- supplied by caller (matches px0.org_id) or generated
  name          VARCHAR(255) NOT NULL,
  status        VARCHAR(20)  NOT NULL DEFAULT 'active',
                -- active | pending_verification | suspended | pending_deletion | deleted
  plan          VARCHAR(50)  NOT NULL DEFAULT 'free',
  source        VARCHAR(30)  NOT NULL DEFAULT 'manual-seed',
                -- px0-bridge | external-admin | self-serve-signup | sso-jit | manual-seed
                -- (matches Contract 13 source enum)
  source_metadata JSONB      NOT NULL DEFAULT '{}',
                -- e.g. { "px0_org_id": "...", "upstream_iss": "https://okta.acme.com" }
  region        VARCHAR(20)  NOT NULL DEFAULT 'us-east-1',
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  suspended_at  TIMESTAMPTZ,
  pending_deletion_at TIMESTAMPTZ,
  deleted_at    TIMESTAMPTZ
);

-- Insert the well-known platform tenant and CI tenant from Contract 13:
INSERT INTO auth.tenants (tenant_id, name, plan, source) VALUES
  ('00000000-0000-0000-0000-000000000001', 'platform',         'enterprise', 'manual-seed'),
  ('00000000-0000-0000-0000-0000000000ff', 'integration-test', 'free',       'manual-seed')
ON CONFLICT (tenant_id) DO NOTHING;
```

> **`plan` ownership (amended 2026-06):** Auth is the INTERIM owner of the tenant plan tier — `tenants.plan` (default `'free'`). It is surfaced (a) as the `plan` claim in agent JWTs (Contract 1) and (b) via the quotas/limits endpoints (Component 1d), so downstream services (rate limits, cache toggles, budgets in Phase 3+) never read `auth.tenants` directly. The LLMs gateway caches the plan for 60s in Valkey. When platform-mgmt/px0 bridge lands (Phase 11), plan ownership migrates there; the column, claim, and `cypherx.tenant.plan_changed` event shapes are unchanged by that migration.

**Lifecycle events emitted (Contract 13 — every source funnels through these):**

```
On tenant insert       → cypherx.tenant.created       (payload: tenant_id, plan, region, source, created_at)
On suspend             → cypherx.tenant.suspended     (payload: tenant_id, reason, suspended_at)
On resume              → cypherx.tenant.resumed       (payload: tenant_id, resumed_at)
On plan change         → cypherx.tenant.plan_changed  (payload: tenant_id, old_plan, new_plan, effective_at)
On soft-delete         → cypherx.tenant.pending_deletion (payload: tenant_id, grace_until)
On hard-delete (after 30d grace) → cypherx.tenant.deleted (payload: tenant_id, deleted_at)
```

The actual first-cycle subscribers to `cypherx.tenant.*` are **LLMs and Guardrails** — each via its own bootstrap-tenant consumer, seeding/cleaning its per-tenant rows on each event (amended 2026-06; the earlier "every SharedCore service subscribes" claim was an overclaim). **RAG deliberately has NO bootstrap-tenant consumer** — it provisions write-through on first touch (a missing `rag.tenant_backends` row resolves as `backend_type='pgvector'`, per the Phase 5 amendment). Services landing later adopt one of these two patterns. **No service subscribes to `px0.*` directly** — the px0-bridge service translates px0 events into `cypherx.tenant.*` (see Phase 11). Self-serve signup (Contract 20) and SSO-JIT also funnel into the same `cypherx.tenant.*` topics.

> **Publication guarantee (amended 2026-06):** every `cypherx.tenant.*` lifecycle event is written to the auth transactional outbox (`auth.outbox` — see *Kafka Events Published by Auth*) in the SAME transaction as the tenant state change. Best-effort log-and-drop publishing is NOT permitted for the provisioning backbone — the ≤5s staleness SLA (Audit Addendum #6) depends on this.

**API Endpoints:**
```
POST   /v1/admin/tenants                  Create tenant         [scope: platform:admin]
GET    /v1/admin/tenants                  List tenants          [scope: platform:admin]
GET    /v1/admin/tenants/{tenant_id}      Get tenant            [scope: platform:admin]
PATCH  /v1/admin/tenants/{tenant_id}/suspend   Suspend tenant   [scope: platform:admin]
PATCH  /v1/admin/tenants/{tenant_id}/resume    Resume tenant    [scope: platform:admin]
DELETE /v1/admin/tenants/{tenant_id}      Soft-delete tenant    [scope: platform:admin]
GET    /v1/tenants/me                     Current tenant info   [scope: tenant:read]
PATCH  /v1/tenants/me                     Update own tenant     [scope: tenant:admin]
```

> The CI integration-test tenant is rejected in `prod` environments (gated by `ENVIRONMENT` env var).

---

### Component 1c — External Onboarding Endpoints ⚡ Partial (NEW — implements Contract 20)

Self-serve sign-up for external developers / customers. Required so that non-px0 customers can become tenants without manual admin intervention.

> **First-cycle slice (amended 2026-06):** signup / verify / resend ONLY, with a config-driven SMTP emitter, a pluggable captcha provider (mock first cycle), and velocity-only risk scoring. On verify, Auth creates the tenant and issues the tenant's **first agent + API key** — there is NO `auth.tenant_users` table and NO session JWT (both deleted; see Amendment Log). `/upgrade` and `/close-account` move to Phase 11; reputation feeds + manual-review UI move to Phase 13.

**Data Model:**
```sql
CREATE TABLE auth.signup_attempts (
  signup_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email                  CITEXT NOT NULL,
  full_name              TEXT NOT NULL,
  intended_use           TEXT,
  terms_version_accepted TEXT NOT NULL,
  verification_token     TEXT NOT NULL UNIQUE,           -- random 32 bytes, hex
  verification_expires_at TIMESTAMPTZ NOT NULL,          -- created_at + 24h
  verified_at            TIMESTAMPTZ,
  tenant_id              UUID,                            -- filled on verification
  initial_agent_id       UUID,                            -- the tenant's first agent, created on verify
                                                          -- (agent-only model — no tenant_users table)
  risk_score             NUMERIC(3,2) NOT NULL DEFAULT 0.00,
  risk_signals           JSONB NOT NULL DEFAULT '{}',     -- first cycle: velocity counters only
                                                          -- (ASN/TLD reputation feeds → Phase 13)
  status                 VARCHAR(30) NOT NULL DEFAULT 'pending_verification',
                         -- pending_verification | manual_review | verified | rejected
  ip_address             INET,
  user_agent             TEXT,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_signup_email ON auth.signup_attempts (email);
CREATE INDEX ix_signup_ip_created ON auth.signup_attempts (ip_address, created_at);
```

**API Endpoints:**
```
⚡ FIRST CYCLE:
POST   /v1/onboarding/signup             Initiate signup           [public, captcha-gated, rate-limited]
GET    /v1/onboarding/verify             Verify email link         [public, token-gated]
POST   /v1/onboarding/resend             Resend verification email [public, rate-limited]

📋 (moved to Phase 11 — see Amendment Log):
POST   /v1/onboarding/upgrade            Promote sandbox → prod    [scope: tenant:admin]
POST   /v1/onboarding/close-account      Tenant termination request [scope: tenant:admin]
```

**Email delivery (config-driven SMTP emitter):**
- Verification and resend emails go through a pluggable emitter selected by env config (`EMAIL_EMITTER=smtp|mock`, `SMTP_URL=...`). Local compose runs MailHog (or the mock emitter, which logs the verification link); a real SMTP relay/SES is the cloud form. No email provider is hardcoded.

**Anti-abuse (first-cycle slice):**
- Disposable-email blocklist consulted on `signup`. List file `auth/data/disposable-domains.txt` refreshed weekly via cron job.
- Per-IP rate limit: 10 signups/hour, enforced by Auth's own Valkey-backed self-protection limits (Component 4, seeded in `auth.rate_limit_config`). Kong perimeter enforcement of the same quota (Contract 19 public route) is the cloud form.
- Captcha: pluggable provider behind a `CaptchaProvider` interface, selected by env config (`CAPTCHA_PROVIDER=mock|turnstile`). First cycle ships the **mock provider** (configurable pass/deny for tests); Cloudflare Turnstile is the production backend, enabled by config later. The verification token MUST be presented in the body and validated by the configured provider before the row is inserted.
- Risk score, first cycle = **velocity-only**: signup velocity per IP, per email domain, and per email address, computed from `auth.signup_attempts`. Score ≥ 0.8 → `status='manual_review'`, no auto-verification email sent. Reputation feeds (TLD/ASN/whois) and the admin manual-review UI are Phase 13 (moved to Phase 13 — see Amendment Log); until then, `manual_review` rows are resolved by a `platform:admin` operator.

**On verification:**
1. `auth.signup_attempts.verified_at = NOW()`.
2. Insert `auth.tenants` row with `source='self-serve-signup'`, `plan='free'`.
3. Seed `auth.tenant_quotas` from `auth.plan_defaults` for `free`.
4. Create the tenant's **first agent** (`auth.agents` row, name `admin`, `allowed_scopes` including `tenant:admin`) and issue its initial API key — the raw key is returned ONCE in the verify response; `signup_attempts.initial_agent_id` records the agent. All subsequent onboarding-UI/API calls use the standard agent-key → JWT exchange (Component 3). No session JWT is issued (the 1h token TTL cap holds).
5. Emit `cypherx.tenant.created` (via the auth transactional outbox, same transaction).

> Earlier drafts also inserted a synthetic `auth.upstream_identity` row (hardcoded-domain issuer), created an `auth.tenant_users` admin row, and issued a 7-day session JWT here. All three are deleted (see Amendment Log): `signup_attempts.tenant_id` already links the signup to its tenant, the platform is agent-only, and no token may exceed the 1h cap.

**On sandbox-to-prod upgrade — 📋 (moved to Phase 11 — see Amendment Log; depends on the billing emitter):**
- Caller posts billing_method choice (`stripe`, `px0`, `manual-invoice`).
- Auth calls the configured billing emitter (Contract 19 / Phase 11) to set up a billing account.
- On success, tenant.plan transitions `free` → `pro` (or chosen plan); `cypherx.tenant.plan_changed` fires.

---

### Component 1d — Per-Tenant Quotas (Contract 19) ⚡ (NEW)

Canonical implementation of the per-tenant quota table referenced in Contract 19.

**Data Model:**
```sql
CREATE TABLE auth.plan_defaults (
  plan          VARCHAR(50) PRIMARY KEY,
  limits        JSONB NOT NULL
);

INSERT INTO auth.plan_defaults (plan, limits) VALUES
  ('free',       '{ /* see Contract 19.2 — modest free-tier values */ }'::jsonb),
  ('pro',        '{ /* mid-tier */ }'::jsonb),
  ('enterprise', '{ /* high-cap */ }'::jsonb)
ON CONFLICT (plan) DO NOTHING;

CREATE TABLE auth.tenant_quotas (
  tenant_id        UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  plan             VARCHAR(50) NOT NULL REFERENCES auth.plan_defaults(plan),
  limits           JSONB NOT NULL,                 -- tenant-specific overrides; merged with plan_defaults
  effective_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  effective_until  TIMESTAMPTZ,
  source           VARCHAR(30) NOT NULL,            -- plan-default | admin-override | billing-event
  updated_by       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, effective_from)
);
CREATE INDEX ix_tenant_quotas_current ON auth.tenant_quotas (tenant_id) WHERE effective_until IS NULL;
```

**API Endpoints:**
```
GET    /v1/admin/tenants/{tenant_id}/quotas    Read effective quotas [scope: platform:admin OR tenant:admin (self)]
PUT    /v1/admin/tenants/{tenant_id}/quotas    Override quotas       [scope: platform:admin]
GET    /v1/quotas                              Caller's own quotas   [scope: tenant:read]
GET    /v1/usage                               Caller's own usage    [scope: tenant:read]
```

> Tenants can read their own quotas/usage; only platform admins can override. Plan changes from the billing system arrive via `cypherx.tenant.plan_changed` and trigger an automatic `tenant_quotas` row insert with `source='billing-event'`.

**`GET /v1/usage` data source (amended 2026-06 — WP04):** the endpoint is backed by an Auth-owned consumer on `cypherx.llms.usage.recorded` that rolls usage into `auth.tenant_usage_counters`. `/v1/usage` reads ONLY this rollup — no cross-schema reads into `llms.*` (Contract 14 single-owner rule), no live joins.

```sql
CREATE TABLE auth.tenant_usage_counters (
  tenant_id    UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  window_start TIMESTAMPTZ NOT NULL,          -- hourly buckets (UTC, truncated)
  metric       VARCHAR(50) NOT NULL,
               -- llm_requests | llm_tokens_in | llm_tokens_out | llm_cost_usd
  value        NUMERIC(20,6) NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, window_start, metric)
);

-- Consumer upsert (idempotent per event via request-correlation de-dup in the consumer):
-- INSERT ... ON CONFLICT (tenant_id, window_start, metric) DO UPDATE SET value = value + EXCLUDED.value
```

**Cache invalidation:** every quota update publishes `cypherx.tenant.plan_changed` on `partition_key=tenant_id`. Every SharedCore service consumes this topic and invalidates its in-process cache for that tenant (TTL 60s anyway, so invalidation is a fast-path optimisation).

---

### Component 1e — Webhook Subscriptions ⚡ (NEW — auth-owned; Contract 21 for signing/retry only)

External customers consume platform events via HTTPS webhooks (they cannot reach our Kafka).

**Data Model (amended 2026-06 — homed in the auth schema):** webhooks are owned end-to-end by Auth: tables `auth.webhook_subscriptions` + `auth.webhook_deliveries`, CRUD endpoints below, and an auth-owned delivery worker. Contract 21 is referenced ONLY for the payload-signing scheme and retry schedule — the previous `platform.webhook_subscriptions` homing (foreign schema, no worker owner) is deleted.

```sql
CREATE TABLE auth.webhook_subscriptions (
  subscription_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  url              TEXT NOT NULL,                  -- HTTPS only
  event_types      TEXT[] NOT NULL,                -- e.g. {'tenant.plan_changed','auth.key.revoked'}
  secret_enc       BYTEA NOT NULL,                 -- HMAC signing secret, envelope-encrypted under the
                                                   -- env-supplied KEK (same pattern as signing keys);
                                                   -- raw secret returned ONCE at create/rotate
  status           VARCHAR(20) NOT NULL DEFAULT 'active',   -- active | paused | deleted
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_webhook_subs_tenant ON auth.webhook_subscriptions (tenant_id) WHERE status = 'active';

CREATE TABLE auth.webhook_deliveries (
  delivery_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id  UUID NOT NULL REFERENCES auth.webhook_subscriptions(subscription_id),
  tenant_id        UUID NOT NULL,
  event_type       VARCHAR(100) NOT NULL,
  payload          JSONB NOT NULL,
  attempt_count    INTEGER NOT NULL DEFAULT 0,
  status           VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | delivered | failed | dead
  last_attempt_at  TIMESTAMPTZ,
  delivered_at     TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_webhook_deliv_pending ON auth.webhook_deliveries (status, last_attempt_at)
  WHERE status IN ('pending','failed');
```

**API Endpoints:**
```
POST   /v1/webhooks                          Create subscription      [scope: tenant:admin]
GET    /v1/webhooks                          List subscriptions       [scope: tenant:admin]
DELETE /v1/webhooks/{id}                     Delete subscription      [scope: tenant:admin]
POST   /v1/webhooks/{id}/rotate-secret       Rotate signing secret    [scope: tenant:admin]
POST   /v1/webhooks/{id}/resume              Reactivate paused        [scope: tenant:admin]
POST   /v1/webhooks/{id}/replay              Replay a stored event    [scope: tenant:admin]
GET    /v1/webhooks/{id}/deliveries          List recent deliveries   [scope: tenant:admin]
```

**Delivery worker (auth-owned):** `auth-webhook-delivery` runs as its OWN compose service (own container; same image as auth-service with a worker entrypoint). It consumes `cypherx.*` topics, fans out to subscriptions matching the `event_types` list, signs the payload per Contract 21's signing scheme, POSTs via a thread pool with Contract 21's retry schedule, and records every attempt in `auth.webhook_deliveries` (dead-lettered to `status='dead'` after the schedule is exhausted; `/resume` + `/replay` operate on these rows).

---

### Component 2 — API Key Management ⚡

**Data Model (PostgreSQL — `auth.api_keys`):**
```sql
CREATE TABLE auth.api_keys (
  key_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id      UUID NOT NULL REFERENCES auth.agents(agent_id),
  tenant_id     UUID NOT NULL,
  key_hash      VARCHAR(64) NOT NULL UNIQUE,
      -- SHA-256 of the raw key — raw key never stored
  key_prefix    VARCHAR(20) NOT NULL,
      -- First 8 chars for display (cx_prod_abc12345...)
  name          VARCHAR(255),
  scopes        TEXT[] NOT NULL DEFAULT '{}',
  status        VARCHAR(20) NOT NULL DEFAULT 'active',
      -- active | revoked | expired
  expires_at    TIMESTAMPTZ,
  last_used_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at    TIMESTAMPTZ,
  revoked_by    UUID
);

CREATE INDEX idx_api_keys_agent_id   ON auth.api_keys(agent_id);
CREATE INDEX idx_api_keys_key_hash   ON auth.api_keys(key_hash);
CREATE INDEX idx_api_keys_tenant_id  ON auth.api_keys(tenant_id);
```

**Key issuance flow:**
```
1. POST /v1/agents/{agent_id}/keys  (request scopes, optional expiry)
2. Auth service generates: raw_key = "cx_" + env + "_" + random_32_bytes_base64url
3. Stores: key_hash = SHA256(raw_key), key_prefix = first 8 chars
4. Returns raw_key ONCE (never stored, never retrievable again)
5. Client stores raw_key securely
```

**API Endpoints:**
```
POST   /v1/agents/{agent_id}/keys          Issue new API key     ⚡
GET    /v1/agents/{agent_id}/keys          List keys (no raw)    ⚡
DELETE /v1/agents/{agent_id}/keys/{key_id} Revoke key            ⚡
POST   /v1/agents/{agent_id}/keys/{key_id}/rotate  Rotate key    📋
```

---

### Component 3 — JWT Issuance ⚡

**Flow:**
```
1. Agent sends: POST /v1/agents/{agent_id}/token
   Body: { "api_key": "cx_prod_...", "scopes": ["llm:invoke", "memory:read"] }

2. Auth service:
   a. Hash the api_key → PLATFORM-SCOPED lookup on api_keys.key_hash (key_hash is
      globally unique; the lookup is NOT tenant-scoped — tenant_id is derived from the
      matched key row. The X-Tenant-ID header is OPTIONAL on this endpoint; if present
      it is cross-checked against the derived tenant_id.)
   b. Verify key status = active, not expired
   c. Compute effective scopes = key.scopes ∩ agent.allowed_scopes ∩ requested_scopes.
      If empty, or does not contain all requested scopes, return 403 FORBIDDEN with the
      specific scope(s) that were filtered out (so clients can diagnose).
   d. Run /authorize check (RBAC: does this agent + tenant have these scopes?)
   e. Mint JWT, RS256, **header `kid` = the active signing key's kid** (Contract 1),
      claims per Contract 1, 1-hour TTL.
   f. Cache the agent's capability set in Valkey (key: agent-caps:{agent_id}, TTL: 5min;
      invalidated on cypherx.auth.agent.updated event).
   g. Return: { "token": "eyJ...", "expires_in": 3600, "token_type": "Bearer" }

3. Agent uses token as Bearer on all subsequent requests.
```

**JWKS Endpoint (consumed by Kong and all services):**
```
GET /.well-known/jwks.json     (no /v1 prefix — RFC 8615 requires the .well-known
                                URI at the origin root. Standard OIDC clients and
                                the Kong JWT plugin assume this path.)
Response:
{
  "keys": [
    {
      "kty": "RSA",
      "kid": "<key-id>",
      "use": "sig",
      "alg": "RS256",
      "n": "<modulus-base64url>",
      "e": "AQAB"
    }
  ]
}
```

> Kong JWT plugin is configured with this JWKS URL. Kong auto-fetches and caches the public key. When Auth rotates keys, Kong picks up the new key within 60s.

**JWKS integrity (NEW — cache-poisoning protection):**

If an attacker can intercept a service's JWKS fetch and swap the response, they can forge tokens that pass signature verification. Two protections, applied at different trust boundaries:

```
1. In-cluster fetches (Kong, every shared-core/xagent/tools service):
   - JWKS URL MUST resolve to the in-cluster Auth Service (.svc.cluster.local).
   - Istio PeerAuthentication is STRICT mTLS across all namespaces (already mandated by
     Cross-Cutting Architecture Invariants). The mTLS handshake authenticates the Auth
     pod's SPIFFE identity (spiffe://cluster.local/ns/shared-core/sa/auth-service).
   - DestinationRule for auth-service enforces serverName check on outbound calls from
     every caller namespace. Plaintext fetches and TLS-without-SPIFFE-identity are denied
     at the sidecar.
   - Net effect: JWKS over HTTP-inside-mTLS is safe; a MITM cannot present the right
     SPIFFE cert.

2. External SDK clients (Python, TypeScript SDK; future external agents):
   - SDKs MUST NOT fetch JWKS over plain HTTPS-PKI alone (CA compromise risk).
   - Auth publishes a SIGNED JWKS BUNDLE at /.well-known/jwks-signed.json:
       { "jwks": <standard jwks doc>,
         "issued_at": "...",
         "expires_at": "...",   // 24h validity
         "signature": "<base64-RS256(canonical-json(jwks+issued_at+expires_at))>",
         "signing_kid": "<root-jwks-signer-kid>" }
   - The root JWKS-signer key is a SEPARATE long-lived offline RSA-4096 key, stored in
     KMS with restricted IAM (only the JWKS-signing job can use it). Public key pinned
     in the SDK at release time and rotated only via SDK version bump.
   - SDK verifies the bundle signature before trusting the embedded JWKS.
   - Bundle is re-signed every 6h by a scheduled Job; sits in S3 + CloudFront for
     low-latency distribution.

Why both: in-cluster has SPIFFE mTLS as the primary defence; external clients have no
SPIFFE relationship to us, so they fall back to a long-lived pinned-root verification.
```

**Failure mode if JWKS-signer root key is compromised:** out-of-band SDK upgrade required (new root key shipped in SDK release; old key deny-listed in next bundle). Acceptable because (a) the root key is offline and (b) signed bundles are short-lived (24h) — an attacker has ≤24h to act before bundle re-signing catches the deny-list.

**Signing key storage — single source of truth (DB only, NOT env vars):**

The previous draft injected `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` as env vars from Doppler. That conflicts with the rotation table — the env var goes stale the moment a new key is inserted, and tokens get signed with a key that's no longer in JWKS. The fix is to make the database the only source of truth.

> **Compose-parity (2026-06):** in the first-cycle runtime the envelope-encryption KEK is supplied via env (`AUTH_SIGNING_KEK`, AES-256-GCM) — the same pattern the LLMs BYOK `sealed:v1` backend reuses. The AWS KMS CMK flow described below is the cloud form (infra phase); table shape, rotation logic, and bootstrap are identical in both forms.

```
auth.signing_keys table holds N signing keys at any time:
  CREATE TABLE auth.signing_keys (
    kid              UUID PRIMARY KEY,
    private_pem_enc  BYTEA       NOT NULL,    -- KMS-encrypted private PEM (envelope encryption)
    public_jwk       JSONB       NOT NULL,    -- public key in JWK format (clear)
    status           VARCHAR(20) NOT NULL,    -- signing | verifying | retired
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_at      TIMESTAMPTZ,             -- when it became the signing key
    retired_at       TIMESTAMPTZ
  );

  -- Exactly one signing key at any moment:
  CREATE UNIQUE INDEX one_signing_key
    ON auth.signing_keys (status)
    WHERE status = 'signing';

KMS:
  Per environment, a Customer-Managed Key:
    alias/cypherx-auth-signing-<env>     (AWS KMS, region-local)
  Auth's IRSA role grants: kms:Decrypt, kms:Encrypt, kms:GenerateDataKey on this CMK only.
  Auth encrypts private PEMs with the CMK before INSERT; decrypts on load.
  Decrypted private keys live in process memory only — never written to disk.
  On KMS outage: Auth continues signing with cached in-memory keys until they retire.
                 /readyz reports degraded only if no signing key is loaded AND KMS unreachable.

Bootstrap:
  On first startup, if auth.signing_keys is empty, Auth generates an RSA-2048 pair,
  KMS-encrypts the private PEM, INSERTs the row with status = 'signing'.

Rotation (every 90 days, or on incident):
  1. Generate new RSA-2048 key pair, KMS-encrypt private PEM.
  2. BEGIN; UPDATE signing_keys SET status='verifying', retired_at=NULL WHERE status='signing';
     INSERT INTO signing_keys (status='signing', promoted_at=NOW(), ...); COMMIT;
     (The partial unique index makes the swap atomic — no window with zero signing keys.)
  3. Auth now signs new tokens with the new kid; old kid remains in JWKS to verify
     in-flight tokens.
  4. After max(JWT TTL) + 1 hour, mark the 'verifying' key as 'retired' and remove from JWKS.

Caches:
  Kong refreshes JWKS every 60s; service in-process JWKS cache TTL = 5 min, refresh-on-miss
  with 1-per-minute rate limit (Contract 1).

Emergency rotation (signing key compromise — NEW):
  Triggered by: suspected key exfiltration, KMS access incident, insider event.
  Steps (single transaction where possible):
    1. POST /v1/admin/signing-keys/emergency-rotate  [scope: platform:admin + emergency token]
       (Interim gate while Component 10 is disabled: platform:admin scope PLUS the
        emergency token read from a mounted secret file (EMERGENCY_ROTATE_TOKEN_FILE),
        presented via X-Emergency-Token. Component 10 step-up approval replaces the
        file gate once the approval flow ships — payments-class action either way.)
    2. Auth:
       a. Generates new RSA-2048 keypair, KMS-encrypts private PEM.
       b. BEGIN;
          UPDATE auth.signing_keys SET status='retired', retired_at=NOW()
            WHERE status IN ('signing','verifying');
          INSERT INTO auth.signing_keys (status='signing', ...);
          COMMIT;
       c. The retired kid is immediately added to Valkey kid-poisoned:{kid}
          with TTL = 1h (max JWT TTL — covers every in-flight token).
       d. Publishes cypherx.auth.key.emergency_revoked Kafka event (alerts SOC).
       e. JWKS endpoint immediately reflects new key; old kid REMOVED (not in
          verifying state — gone). Standard rotation keeps old kid for verification;
          emergency rotation does NOT.
       f. Re-signs the /.well-known/jwks-signed.json bundle out-of-cycle.
    3. Every verifier (already subscribed to Kafka cypherx.auth.token.revoked semantics
       and kid-poisoned Valkey checks) immediately rejects tokens with the bad kid,
       even if their signature would otherwise verify.
    4. All in-flight requests using bad-kid tokens fail 401 KEY_REVOKED → clients
       call /v1/agents/{id}/token to re-mint.

Time to global effect: ≤ 60s (Kong JWKS refresh + Valkey propagation).
Time to total token expiry of compromised key: ≤ 1h (JWT TTL).

Drill cadence: rehearsed every quarter on staging. Test case: forge a token with the
old kid AFTER emergency rotation; assert every verifier rejects with KEY_REVOKED.
```

---

### Component 3b — Token Binding & Replay Mitigation 📋 (NEW — addresses Problem 5)

> **Why this matters:** Bearer tokens are stealable. Today, anyone in possession of a valid JWT *is* the agent — there is nothing tying the token to the client, the workload, or the TLS connection that presented it. For an autonomous-agent platform where tokens flow through tool servers, SDKs, A2A hops, and external callbacks, that is the highest-leverage class of credential theft.
>
> The architecture below adopts the standard solution (RFC 7800 / 8705 / 9449) and is staged so the first-cycle implementation pays no cost and the production hardening migration is a single cutover.

**First-cycle (⚡) minimums — implement now, no perf cost:**

1. **`jti` single-use semantics — one-time credentials ONLY (rescoped 2026-06):** single-use `jti` enforcement (reject re-presentation with 401 `TOKEN_REPLAYED`) applies exclusively to one-time credentials: future DPoP proofs (Phase 13 table below), Component 10 approval tokens, and optionally service-token nonces. Bearer agent JWTs are multi-use by design — a 1-hour token is re-presented on every request — and are NEVER subject to a replay window (the previous "every service maintains `jti-seen`" rule would have broken all four services after each token's first use; deleted, see Amendment Log). The bearer kill-switch need is covered by Component 3c `jti` revocation, which IS enforced verifier-side (WP03).
2. **`cnf` claim reserved but unused** — Contract 1 reserves the `cnf` (confirmation, RFC 7800) claim. First-cycle tokens omit it; verifiers MUST accept either presence (verify binding) or absence (skip binding). This avoids a breaking change when Phase 13 turns binding on.
3. **TLS session pinning for streaming endpoints** — any SSE/WS stream session is bound to the JWT `jti` it was opened with. Re-presenting the same token over a different TLS session breaks the stream. (Cheap; close protection on the only long-lived auth surface. This is session binding, not a replay window — the same token remains valid for new requests.)

**Phase 13 hardening (📋) — required before external SDK GA:**

| Client class | Binding mechanism | `cnf` claim shape |
|--------------|------------------|---------------------|
| Internal services (in-cluster) | **mTLS-bound token (RFC 8705)** via Istio SPIFFE SVID | `cnf: { "x5t#S256": "<sha256-of-presented-client-cert>" }` |
| External SDK clients (Python/TS) | **DPoP (RFC 9449)** — client signs each request with a per-session keypair; key thumbprint baked into token | `cnf: { "jkt": "<base64url-jwk-thumbprint>" }` |
| Internal agents running on K8s | **SPIFFE SVID-bound** — same as mTLS; SVID is workload's identity | `cnf: { "x5t#S256": "<sha256-of-spiffe-cert>" }` |

**Verification flow (every service that accepts a JWT):**

```
On request inbound:
  1. Verify signature, claims (Contract 1).
  2. If cnf.x5t#S256 present:
       compute sha256 of mTLS-presented client cert (Istio sidecar exposes via X-Forwarded-Client-Cert).
       MUST equal cnf.x5t#S256. Else 401 TOKEN_BINDING_MISMATCH.
  3. If cnf.jkt present:
       require DPoP header signed with key whose thumbprint == cnf.jkt.
       Verify DPoP signature, htu (URI), htm (method), iat (≤ 60s old), jti (replay window
       — the DPoP PROOF jti is one-time; this is where single-use jti semantics live).
       Else 401 TOKEN_BINDING_MISMATCH.
  4. jti revocation check against Component 3c (always, even without cnf).
     NOTE (2026-06): the bearer JWT's own jti is checked against the REVOCATION list only —
     never against a replay window. Single-use rejection applies solely to one-time
     credentials (DPoP proof jti in step 3, approval tokens).
```

**Migration cutover (Phase 13):**
- Auth begins minting tokens with `cnf` populated based on the requesting client's class.
- All services already accept-with-or-without; once Auth flips the switch, every new token carries binding.
- Old unbound tokens age out naturally within 1h (max JWT TTL).
- Bootstrap-secret service tokens (Contract 12 first-cycle path) **must** be retired in this cutover — they cannot be bound.

---

### Component 3c — Live Token Revocation ⚡ (NEW)

> **Why this matters:** Today, the only way to "revoke" an agent's access is to deactivate the agent or revoke its API key — but **already-issued JWTs remain valid until `exp`** (up to 1 hour). For incident response (compromised agent, leaked token, policy violation requiring immediate stop), that is too slow. The fix is a `jti`-based revocation list that every verifier checks — enforced verifier-side in every service (WP03). This is the bearer kill-switch; it does not make bearer `jti`s single-use (see Component 3b, amended).

**Architecture: Valkey + Kafka, no DB read on the hot path.**

```
auth.revoked_tokens table (PostgreSQL — durable record):
  CREATE TABLE auth.revoked_tokens (
    jti          UUID PRIMARY KEY,
    agent_id     UUID,                          -- nullable; set for agent-token revocation
    tenant_id    UUID NOT NULL,
    revoked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_by   UUID NOT NULL,                 -- user_id from px0 OR agent_id of admin
    reason       VARCHAR(50) NOT NULL,
                  -- compromised | rotated | deactivated | policy_violation | admin_action
    token_exp    TIMESTAMPTZ NOT NULL           -- original exp; row purged after this time
  );
  CREATE INDEX idx_revoked_purge ON auth.revoked_tokens(token_exp);

Valkey (hot path — every verifier checks here):
  Key pattern:   jti-revoked:{jti}
  Value:         "1"
  TTL:           remaining time until token_exp (auto-expires when token would naturally die)

Kafka topic: cypherx.auth.token.revoked
  payload: { jti, agent_id, tenant_id, token_exp, reason, revoked_at }
  Every verifier subscribes (one consumer group per service) and primes its own
  in-process bloom filter (1MB, ~10M entries, 1% false-positive rate).
```

**Hot-path verification flow (every service that accepts an agent JWT):**

```
1. Verify signature + claims (existing flow).
2. Check bloom filter: if jti is "possibly revoked", continue to step 3.
   If "definitely not revoked", skip to step 4.
3. EXISTS jti-revoked:{jti} in Valkey.
   If true → 401 TOKEN_REVOKED.
4. Proceed with request.

Bloom filter eliminates the Valkey round-trip for the ~99% of tokens that are not revoked.
False positives degrade to a Valkey check (still <1ms).
```

**Revocation API:**

```
POST /v1/tokens/revoke                              [scope: agent:revoke OR platform:admin]
  Body: { "jti": "<uuid>", "reason": "compromised" }
  Effect: insert into auth.revoked_tokens + auth.outbox row (same transaction; relay
          publishes cypherx.auth.token.revoked), SETEX in Valkey.
  Returns: 204 No Content.

POST /v1/agents/{agent_id}/revoke-all-tokens        [scope: agent:revoke OR platform:admin]
  Body: { "reason": "compromised" }
  Effect:
    1. Query Valkey set agent-active-jtis:{agent_id} (populated at /token issuance,
       TTL = JWT TTL) for all live jti.
    2. For each jti: insert auth.revoked_tokens, SETEX, publish.
    3. Mark auth.agents.status = 'suspended' (prevents new tokens).
  Returns: { "revoked_count": N }

POST /v1/tenants/{tenant_id}/revoke-all-tokens      [scope: platform:admin]
  Effect: bulk revoke every live jti for tenant.
  Use case: tenant suspension event from px0.
```

**Active-jti tracking (required for revoke-all-tokens):**

```
On every /v1/agents/{id}/token issuance:
  SADD agent-active-jtis:{agent_id} {jti}
  EXPIRE agent-active-jtis:{agent_id} (max of existing TTL, new jwt_exp - now)

On natural expiry, jti auto-falls-out (no cleanup needed because the set TTL covers it).
```

**Cascade rules (automatic revocation triggers):**

| Trigger | Effect |
|---------|--------|
| API key revoked (Component 2) | All live jtis minted from that key revoked |
| Agent deactivated (`status = inactive`) | All live jtis for agent revoked |
| Agent quarantined (Component 5c) | All live jtis revoked; new tokens blocked until cooldown |
| Tenant suspended (px0 event) | All live jtis for tenant revoked |
| Signing key compromised (Component 3 emergency rotation) | All jtis signed with that kid added to a poisoned-kid revocation rule |

**Poisoned-kid handling (signing key compromise):**

```
Separate Valkey key: kid-poisoned:{kid}  TTL = max JWT TTL (1h)
Verifier check (after signature verify, before jti revocation check):
  EXISTS kid-poisoned:{header.kid} → 401 KEY_REVOKED, even if signature was valid.

This is the only way to revoke en-masse when individual jti enumeration is infeasible
(e.g., the key has signed millions of in-flight tokens).
```

**Purge job:**

```
Hourly background job:
  DELETE FROM auth.revoked_tokens WHERE token_exp < NOW() - INTERVAL '1 hour';
  (The +1h buffer keeps rows queryable for incident forensics just past expiry.)
```

**Audit:**

- Every revoke writes an `auth.audit_log` row with `event_type = token.revoked` and a `reason`.
- Bulk revokes write one row per revoked jti (for forensic precision), batched via COPY.

**First-cycle stance:**

- ⚡ first cycle: ship `auth.revoked_tokens` table, `POST /v1/tokens/revoke`, `POST /v1/agents/{id}/revoke-all-tokens`, Valkey check in every verifier, Kafka topic, bloom-filter primed from Kafka replay on startup.
- 📋 Phase 13: tenant-wide revocation endpoint (depends on px0 bridge); cross-cluster revocation propagation (depends on multi-region).

---

### Component 4 — Authorization Endpoint ⚡

**What it is:** The single endpoint that every service calls before performing a protected action.

```
POST /v1/authorize

Required header: X-Forwarded-Agent-JWT: <agent-jwt>
                 (Auth extracts agent_id AND tenant_id from this JWT — never from the body.
                  See Contract 13 anti-pattern.)

Request body (note: NO tenant_id, NO agent_id — those come from the JWT):
{
  "action":     "llm:invoke",
  "resource":   "model:claude-sonnet-4-6",
  "context":    { "plan": "pro" }
}

If the body contains agent_id or tenant_id, return 400 BAD_REQUEST.

Response (allowed):
{
  "allowed":    true,
  "reason":     null,
  "policy_ids": ["default-llm-policy"]
}

Response (denied):
{
  "allowed":    false,
  "reason":     "Agent does not have llm:invoke scope",
  "policy_ids": ["default-llm-policy"]
}
```

**Decision logic:**
```
1. Verify X-Forwarded-Agent-JWT signature + claims. Extract agent_id, tenant_id, scopes.
2. Load agent's allowed_scopes from Valkey cache (fallback: DB).
3. Load tenant status from auth.tenants. If suspended/deleted, deny immediately.
4. Check: requested action ∈ agent's scopes.
5. Check: RBAC policy for (tenant_plan + action) — loaded from policy cache.
6. Check: ABAC rules (attribute conditions) — if any apply.
7. Log decision to auth.audit_log table.
8. Return allow/deny.
```

**Self-protection rate limits (NEW — Auth's own endpoints):**

Kong rate-limits external traffic, but **internal callers** hitting `/v1/authorize`, `/v1/service-tokens`, or `/v1/agents/{id}/token` have no ceiling. A compromised in-cluster service in a hot loop can DoS Auth from inside the mesh — past Kong, past the perimeter. Auth therefore enforces its own limits in front of every endpoint, Valkey-backed:

```
Per-endpoint quotas (in addition to Kong perimeter limits):

  POST /v1/authorize:
    Per-caller-service:   5000 rpm  (Auth tracks via service-JWT sub claim)
    Per-tenant:           2000 rpm
    Burst:                2x for 10s
    Excess: 429 RATE_LIMIT_EXCEEDED, Retry-After header

  POST /v1/agents/{id}/token:
    Per-agent:            60 rpm    (token issuance should be rare; spike = compromise signal)
    Per-tenant:           600 rpm
    Burst:                2x for 30s
    Excess at agent level for >5 min → publish cypherx.auth.suspicious.token_burst event
    (Component 5c may quarantine the agent automatically.)

  POST /v1/service-tokens:
    Per-service (X-Service-Name): 30 rpm
    (Services renew tokens 120s before expiry — 30 rpm is 30x the legitimate rate.)
    Excess: 429 + alert to SOC (service auth never legitimately bursts).

  POST /v1/admin/*:
    Per-admin-agent:      10 rpm
    Excess: 429 + ALL admin actions audited at INFO level always.

Keys:
  rl-auth:{endpoint}:{tenant_id or caller}:{minute-bucket}
  Atomic INCR + EXPIRE 60s.

Failure mode (Valkey outage):
  Self-protection limits fail OPEN with WARN log (same rationale as Component 5c —
  locking out the platform on cache outage causes more damage than it prevents).
  However: a hard upper bound applies — Auth refuses > 50,000 rpm total across all
  endpoints regardless of Valkey state (in-process counter, last-resort circuit
  breaker against DB exhaustion).
```

These limits live in `auth.rate_limit_config` (tenant-overridable for enterprise plans) and hot-reload from a Kafka `cypherx.auth.config.updated` event.

**DDL + seed (added 2026-06 — this table was previously referenced with no DDL):**

```sql
CREATE TABLE auth.rate_limit_config (
  config_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  endpoint         VARCHAR(100) NOT NULL,   -- '/v1/authorize' | '/v1/agents/{id}/token' |
                                            -- '/v1/service-tokens' | '/v1/admin/*' |
                                            -- '/v1/onboarding/signup'
  scope_kind       VARCHAR(30)  NOT NULL,   -- per-caller-service | per-tenant | per-agent |
                                            -- per-service | per-admin-agent | per-ip
  tenant_id        UUID,                    -- NULL = platform default; non-NULL = enterprise override
  limit_rpm        INTEGER NOT NULL,
  burst_multiplier NUMERIC(4,2) NOT NULL DEFAULT 1.00,
  burst_seconds    INTEGER NOT NULL DEFAULT 0,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE NULLS NOT DISTINCT (endpoint, scope_kind, tenant_id)   -- PG15+; platform-default rows are unique
);

-- Seed: platform-default rows mirroring the quota table above (idempotent):
INSERT INTO auth.rate_limit_config (endpoint, scope_kind, limit_rpm, burst_multiplier, burst_seconds) VALUES
  ('/v1/authorize',            'per-caller-service', 5000, 2.00, 10),
  ('/v1/authorize',            'per-tenant',         2000, 2.00, 10),
  ('/v1/agents/{id}/token',    'per-agent',            60, 2.00, 30),
  ('/v1/agents/{id}/token',    'per-tenant',          600, 2.00, 30),
  ('/v1/service-tokens',       'per-service',          30, 1.00,  0),
  ('/v1/admin/*',              'per-admin-agent',      10, 1.00,  0),
  ('/v1/onboarding/signup',    'per-ip',               10, 1.00,  0)   -- 10/hour ≈ enforced on hour bucket
ON CONFLICT (endpoint, scope_kind, tenant_id) DO NOTHING;
```

**Valkey caching:**
```
Key: authz:{tenant_id}:{agent_id}:{action}:sha256(resource_str || canonical_json(context))
TTL: 30 seconds

Invalidation (defence in depth):
  - 30s natural TTL handles policy changes within a bounded window.
  - On policy update, Auth publishes cypherx.auth.policy.changed with affected tenant_id.
  - On agent scope change, Auth publishes cypherx.auth.agent.updated with agent_id.
  - Each service maintains an L2 in-process cache (5s) that subscribes to these events
    and flushes the matching prefix.
  - The 30s + L2 5s + pub/sub combination bounds staleness to the smaller of the two
    TTLs in the worst case.
```

---

### Component 5 — RBAC Policy Engine ⚡ (Basic)

**Data Model (PostgreSQL — `auth.policies`):**
```sql
CREATE TABLE auth.policies (
  policy_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID,         -- NULL = platform default policy
  name         VARCHAR(255) NOT NULL,
  description  TEXT,
  version      INTEGER NOT NULL DEFAULT 1,
  status       VARCHAR(20) NOT NULL DEFAULT 'active',
  rules        JSONB NOT NULL,
      -- Array of: { action, resource_pattern, effect: "allow"|"deny", conditions: [] }
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**First cycle policy rules (seeded as ONE platform-default row, `tenant_id IS NULL`):**

> One row, `tenant_id IS NULL` (platform default), `name = 'default-allow-first-cycle'`. The decision engine evaluates `WHERE tenant_id = $1 OR tenant_id IS NULL ORDER BY tenant_id NULLS LAST` so a per-tenant override (added later) wins over the platform default.

```json
[
  { "action": "llm:invoke",       "effect": "allow", "conditions": [] },
  { "action": "memory:read",      "effect": "allow", "conditions": [] },
  { "action": "memory:write",     "effect": "allow", "conditions": [] },
  { "action": "rag:query",        "effect": "allow", "conditions": [] },
  { "action": "tool:invoke",      "effect": "allow", "conditions": [] },
  { "action": "guardrails:check", "effect": "allow", "conditions": [] }
]
```

---

### Component 5c — Behavioral Constraints Engine 📋 (NEW — addresses Problem 1)

> **Why this matters:** RBAC + ABAC answer "is this action permitted by policy?" They do not answer "is this agent behaving like itself right now?" An agent that has been compromised, jailbroken, or stuck in a runaway loop will hold a perfectly valid JWT and walk through every scope check. The single biggest gap in static-authorization models for autonomous-agent platforms is the absence of *runtime behavioral envelopes*.
>
> The Behavioral Constraints Engine sits **alongside** RBAC/ABAC, not inside them. RBAC/ABAC answer in microseconds (cached). Behavioral checks answer in single-digit milliseconds (Valkey counters). Both must allow before an action proceeds.

**Data model (PostgreSQL — `auth.behavior_policies`):**

```sql
CREATE TABLE auth.behavior_policies (
  policy_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID,         -- NULL = platform default
  agent_id      UUID,         -- NULL = applies to all agents in tenant
  name          VARCHAR(255) NOT NULL,
  version       INTEGER NOT NULL DEFAULT 1,
  status        VARCHAR(20)  NOT NULL DEFAULT 'active',
                -- active | shadow | suspended
                -- shadow = evaluate + log but do not enforce (for tuning)
  constraints   JSONB NOT NULL,
  enforcement   VARCHAR(20)  NOT NULL DEFAULT 'block',
                -- block | quarantine | alert
                -- block      = reject the action
                -- quarantine = mark agent suspended for cooldown_seconds, alert SOC
                -- alert      = allow, emit high-severity event, write audit row
  cooldown_seconds INTEGER NOT NULL DEFAULT 300,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_behavior_tenant ON auth.behavior_policies(tenant_id, agent_id) WHERE status = 'active';
```

**Constraints schema (Contract 17, see Phase 0):**

```json
{
  "rate_limits": {
    "tool_calls_per_minute":      50,
    "memory_reads_per_minute":    1000,
    "memory_writes_per_minute":   100,
    "llm_calls_per_minute":       30,
    "a2a_delegations_per_task":   10,
    "parallel_tasks":             5
  },
  "structural_limits": {
    "max_recursive_depth":           5,
    "max_subagent_spawn_per_task":   3,
    "max_tool_call_chain_length":   20
  },
  "sequence_rules": [
    {
      "name": "research-pattern",
      "allowed_sequence": ["tool:web-search", "tool:http-fetch", "llm:invoke"],
      "violation_action": "alert"
    },
    {
      "name": "no-write-after-external-read",
      "forbid_sequence": ["tool:http-fetch", "memory:write"],
      "violation_action": "block"
    }
  ],
  "anomaly_signals": {
    "token_burn_rate_per_hour_usd":     5.00,
    "tool_call_entropy_threshold":      0.85,
    "novel_tool_invocation_threshold":  3
  }
}
```

**Enforcement architecture:**

```
Every service that accepts agent actions wraps its handler with a behavior-check middleware:

  on_action(agent_id, action, context):
    1. Lookup applicable behavior policy in Valkey cache (key: behav:{tenant_id}:{agent_id}, TTL 30s).
    2. For each rate_limit relevant to `action`:
         INCR Valkey counter at behav-cnt:{agent_id}:{action}:{minute-bucket}
         EXPIRE to current minute + 60s
         If counter > limit → enforcement action (block / quarantine / alert).
    3. For each sequence_rule relevant to `action`:
         Read last N actions from Valkey list behav-seq:{agent_id} (LRANGE 0 19).
         If sequence matches forbid_sequence → enforcement action.
         LPUSH new action; LTRIM 0 19 (sliding window of last 20).
    4. For anomaly signals: compute over rolling window in Valkey, compare threshold.
    5. If all pass → proceed; else return 429 BEHAVIORAL_LIMIT or 423 AGENT_QUARANTINED.
```

**Quarantine handling:**

```
When enforcement = quarantine:
  1. UPDATE auth.agents SET status='quarantined', quarantine_until=NOW()+cooldown_seconds.
  2. Publish cypherx.auth.agent.quarantined Kafka event.
  3. Auth /v1/agents/{id}/token returns 423 QUARANTINED with quarantine_until until expiry.
  4. In-flight tasks owned by this agent receive cancellation signal (Phase 10 Component 5b).
  5. After cooldown, status auto-flips back to 'active'. Human review required if quarantine count > 3 in 24h.
```

**Authoritative staging (amended 2026-06 — supersedes all other scope statements for 5c):**

- ⚡ Phase 2 (first cycle): the `auth.behavior_policies` TABLE + ONE seeded platform-default policy row (`status='shadow'`, `enforcement='alert'`) — and nothing else. NO middleware ships in this phase; the seed row exists so later phases have a stable policy shape to evaluate against.
- 📋 Phase 10: the Valkey counter middleware in alert-only mode across services (every breach logged, nothing blocked) — baseline + 30-day tuning window.
- 📋 Phase 13: blocking/quarantine enforcement, per-tenant policies, sequence rules, anomaly signals, ML-based novelty scoring (off-line trained on historical action streams).

> The enforcement-architecture and quarantine sections above describe the full design; the middleware they specify lands in Phase 10 (alert-only) and Phase 13 (blocking), per this staging.

**Counter durability:**

- Counters are Valkey-only. On Valkey outage, behavior checks **fail open with an alert** (logged as `WARN behavioral_check_unavailable`). The reasoning: locking out every agent on cache infra outage causes more incident than it prevents. The trade-off is documented per Cross-Cutting #4 (fail-mode is explicit).

**Why this is separate from RBAC:**

- RBAC answers "is the scope present?" — boolean, cached 30s, decision-graph evaluation.
- Behavioral answers "is the *rate* and *sequence* of permitted actions still sane?" — stateful, rolling-window, cannot be cached.
- An agent with `tool:invoke` scope and a clean RBAC record can still call `tool-web-search` 10,000 times in 60 seconds — only this engine catches that.

---

### Component 6 — Audit Log ⚡

**Data Model (PostgreSQL — `auth.audit_log`):**
```sql
CREATE TABLE auth.audit_log (
  id           BIGSERIAL PRIMARY KEY,
  event_type   VARCHAR(50) NOT NULL,
      -- agent.registered | key.issued | key.revoked | token.issued | authz.allowed | authz.denied
      -- approval.requested | approval.granted | approval.consumed | token.revoked | ...
  agent_id     UUID,
  tenant_id    UUID NOT NULL,
  action       VARCHAR(100),
  resource     VARCHAR(255),
  decision     VARCHAR(10),    -- allow | deny
  policy_ids   TEXT[],
  request_id   UUID,
  trace_id     UUID,
  ip_address   INET,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Tamper-evidence (NEW):
  row_hash     BYTEA NOT NULL,
      -- sha256(canonical_json(event_type|agent_id|tenant_id|action|resource|decision|
      --        policy_ids|request_id|trace_id|ip_address|created_at) || prev_row_hash)
  prev_row_hash BYTEA NOT NULL
      -- references previous row in the per-tenant hash chain; genesis row uses zero-hash
);

CREATE INDEX idx_audit_tenant_id  ON auth.audit_log(tenant_id, created_at DESC);
CREATE INDEX idx_audit_agent_id   ON auth.audit_log(agent_id, created_at DESC);
CREATE INDEX idx_audit_event_type ON auth.audit_log(event_type, created_at DESC);

-- Append-only enforcement (defence against direct DB tampering):
REVOKE UPDATE, DELETE ON auth.audit_log FROM auth_service_runtime;
-- Only the migration role and a dedicated retention-purge role retain those grants.
```

**Tamper-evidence design (NEW):**

```
Hash chain (per tenant — independent chains avoid cross-tenant write serialisation):
  prev_row_hash of row N = row_hash of row N-1 for same tenant_id.
  row_hash of row N      = sha256(canonical_payload(N) || prev_row_hash(N)).
  Genesis row per tenant uses prev_row_hash = 32 zero bytes.

  In-process: Auth service maintains a Valkey key audit-chain-tip:{tenant_id} with the
  latest row_hash. INSERT path:
    1. WATCH audit-chain-tip:{tenant_id}
    2. Read tip → compute new row_hash → INSERT row with that prev_row_hash + row_hash
    3. SET audit-chain-tip:{tenant_id} to new row_hash (MULTI/EXEC)
    On Valkey miss: fall back to SELECT row_hash FROM auth.audit_log WHERE tenant_id=$1
    ORDER BY id DESC LIMIT 1 FOR UPDATE.

Verification: any auditor can replay the chain from the genesis row forward; if any row
was modified or deleted, the chain breaks. CI runs a verification job hourly and alerts
on break.

S3 mirror (immutable backup):
  Every audit row is also published to Kafka cypherx.platform.audit.event (already
  in the Enterprise Flow doc). A consumer writes to s3://cypherx-audit-<env>/ in
  Parquet files keyed by tenant_id + hour bucket. S3 bucket has Object Lock in
  Governance mode (90 days minimum; tenant-configurable up to 7 years for HIPAA).
  After the lock window, only platform:audit-purge scope can delete.

  Consequence: even if an attacker compromises Auth's DB and rewrites auth.audit_log,
  the S3 mirror retains the original chain. Auditor compares DB chain vs S3 chain;
  divergence triggers an INCIDENT_CRITICAL alert.
```

**Retention:**
- Hot tier (`auth.audit_log` in PostgreSQL): 90 days, partitioned by month (📋 follow-up below).
- Cold tier (S3 Parquet under Object Lock): default 90 days; per-tenant config up to 7 years.
- Purge in hot tier is a separate role (`auth_audit_purge`) running a nightly job; the runtime role cannot DELETE.

> **Secret redaction (MUST enforce in the logger middleware):**
> The audit log and access log MUST redact the following JSON paths from request bodies before persisting / emitting:
> - `$.api_key`, `$.bootstrap_secret`, `$.password`, `$.private_pem`, `$.private_key`, `$.token`
>
> And the following request headers:
> - `Authorization`, `X-Service-Bootstrap-Secret`, `X-Bootstrap-Token`, `X-Forwarded-Agent-JWT`
>
> A CI test loads the logger with a fixture request containing each of these fields and asserts the emitted log entry has them replaced with `"***REDACTED***"`. PR cannot merge without this test passing.

> **Partitioning (📋 follow-up):** `auth.audit_log` is high-write. Partition by month (`PARTITION BY RANGE (created_at)`) once first-cycle data volume is measured. Out of scope for ⚡.

> **Table scope annotations (resolves Contract 13 ambiguity):**
> - Tenant-scoped (require `tenant_id` + RLS): `auth.agents`, `auth.api_keys`, `auth.audit_log`, `auth.policies` (per-tenant rows where `tenant_id IS NOT NULL`).
> - Platform-scoped (no `tenant_id`, no RLS): `auth.signing_keys`, `auth.service_acl`, `auth.bootstrap_state`, `auth.tenants`, `auth.policies` (platform default rows where `tenant_id IS NULL`).
> - The platform-scoped tables are mutated only by Auth itself (DDL user or `platform:admin` agent). Direct DB access from other services to these tables is forbidden.

**Audit log READ API ⚡ (NEW — required for SOC 2 / HIPAA / GDPR compliance):**

External tenants and platform admins need to retrieve their audit history. Without an API, the chain is unauditable from the customer's perspective.

```
GET /v1/audit-log?from=<iso8601>&to=<iso8601>&event_type=<...>&agent_id=<...>&cursor=<...>&limit=<...>
  Returns the caller's tenant's audit log entries (RLS enforces tenant filter).
  Cursor-paginated (Contract 9). Default limit 50, max 500.
  Caller scope: tenant:admin (own tenant) OR platform:admin (any tenant via ?tenant_id=...).

GET /v1/audit-log/verify?from=<iso8601>&to=<iso8601>
  Re-walks the per-tenant hash chain over the requested window and returns:
    { "ok": true,  "rows_verified": 12345, "from_hash": "...", "to_hash": "..." }
    { "ok": false, "broken_at_row_id": 999, "expected_prev_hash": "...", "actual_prev_hash": "..." }
  Customers and auditors use this to verify tamper-evidence themselves.

GET /v1/audit-log/export?from=<iso8601>&to=<iso8601>
  Initiates a long-running export job (S3 pre-signed URL in callback or response).
  Format: JSONL or Parquet. TTL 7 days. Caller scope: tenant:admin.
```

---

### Component 7 — Secret Rotation 📋

**What it is:** Zero-downtime API key rotation. During rotation, both old and new keys are valid for a configurable window (default: 24h), then the old key is revoked.

**Flow:**
```
POST /v1/agents/{agent_id}/keys/{key_id}/rotate
  1. Issue new key (status: active)
  2. Mark old key: status = rotating, rotate_expires_at = NOW() + 24h
  3. Return new key
  4. Background job: at rotate_expires_at, set old key status = revoked
  5. Event published: cypherx.auth.credential.rotated
```

---

### Component 8 — A2A Trust (Agent-to-Agent JWT) 📋

**What it is:** When Agent A delegates a task to Agent B, Agent A presents a signed identity token proving its identity and the delegation scope.

**Additional token type:**
```
POST /v1/agents/{agent_id}/a2a-token
Body: {
  "target_agent_id": "<uuid>",
  "delegated_scopes": ["llm:invoke"],
  "task_id": "<uuid>",
  "ttl_seconds": 300
}

Returns a short-lived JWT (5 min TTL) with extra claims:
  "delegation_from": "<sender-agent-id>"
  "delegation_task": "<task-id>"
  "delegation_scope": ["llm:invoke"]
```

---

### Component 8b — Service Token Issuance ⚡ (NEW)

**What it is:** The endpoint that lets internal services obtain the short-lived service JWTs defined in Contract 12. This unblocks every first-cycle inter-service call.

```
POST /v1/service-tokens

Authentication (FIRST CYCLE — bootstrap-secret only, per Contract 12 decision):
  Headers:
    X-Service-Bootstrap-Secret: <doppler:service-auth/<service-name>/bootstrap_secret>
    X-Service-Name:             <service-name>     (e.g. "xagent")
  Auth verifies the bootstrap secret matches the Doppler-sourced value for that service.

Phase 13 hardening (NOT first cycle): replace bootstrap-secret with K8s TokenReview +
SPIFFE identity. Single-cutover migration; the two modes do not run concurrently.

Request body (audience omitted — Contract 12 decision: first cycle mints aud=["*"]):
  {
    "tenant_id":    "<uuid>",          // tenant on whose behalf the call is made
    "on_behalf_of": "<agent-uuid>",    // optional — the agent that triggered this call
    "ttl_seconds":  300                // max 300 (matches Contract 12 5-minute spec)
  }

Server-side derivation (caller does NOT specify scopes):
  scopes = union of allowed_scopes from auth.service_acl WHERE caller_service = X-Service-Name
  If the caller has no service_acl row at all, return 403 FORBIDDEN.

Response:
  { "token": "eyJ...", "expires_in": 300, "kid": "<kid>", "aud": ["*"] }

Audit: every issuance logged in auth.audit_log with event_type = service_token.issued.
```

**Service-token allow-list (controls which services can talk to which):**
```sql
CREATE TABLE auth.service_acl (
  caller_service  VARCHAR(100) NOT NULL,
  target_service  VARCHAR(100) NOT NULL,
  allowed_scopes  TEXT[] NOT NULL,
  PRIMARY KEY (caller_service, target_service)
);
```

**Seed migration `auth/db/migrations/*__seed_service_acl.sql` (first-cycle edges, idempotent):**

| caller_service     | target_service     | allowed_scopes                       |
|--------------------|--------------------|--------------------------------------|
| xagent             | auth-service       | `[internal:read]`                    |
| xagent             | llms-gateway       | `[internal:read, internal:write]`    |
| xagent             | guardrails-service | `[internal:read, internal:write]`    |
| llms-gateway       | auth-service       | `[internal:read]`                    |
| guardrails-service | auth-service       | `[internal:read]`                    |

```sql
INSERT INTO auth.service_acl (caller_service, target_service, allowed_scopes) VALUES
  ('xagent',             'auth-service',       ARRAY['internal:read']),
  ('xagent',             'llms-gateway',       ARRAY['internal:read','internal:write']),
  ('xagent',             'guardrails-service', ARRAY['internal:read','internal:write']),
  ('llms-gateway',       'auth-service',       ARRAY['internal:read']),
  ('guardrails-service', 'auth-service',       ARRAY['internal:read'])
ON CONFLICT (caller_service, target_service) DO NOTHING;
```

Service names align with ECR repo names (Phase 1 Component 5): `auth-service`,
`llms-gateway`, `guardrails-service`, `xagent`, etc.

This table is the enforceable counterpart to the Istio AuthorizationPolicy (network-level) — together they implement defence in depth.

---

### Component 8b-ext — OAuth2 `client_credentials` for External Services ⚡ (NEW — implements Contract 12 mode 3)

External customer backends need a service identity but have no SPIFFE/Doppler path. They register a "service client" and exchange `client_id + client_secret` (or a federated OIDC `client_assertion`) for a short-lived service JWT.

**Data Model:**
```sql
CREATE TABLE auth.service_clients (
  client_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            UUID NOT NULL REFERENCES auth.tenants(tenant_id) ON DELETE CASCADE,
  name                 TEXT NOT NULL,
  client_secret_hash   TEXT,                         -- Argon2id; NULL if federated-only
  allowed_grant_types  TEXT[] NOT NULL DEFAULT '{client_credentials}',
  allowed_audiences    TEXT[] NOT NULL,              -- target service names this client may call
  allowed_scopes       TEXT[] NOT NULL,              -- subset of internal:* and service-specific scopes
  status               VARCHAR(20) NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'rotating', 'revoked')),
  created_by           UUID NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at           TIMESTAMPTZ,
  last_used_at         TIMESTAMPTZ
);
CREATE INDEX ix_service_clients_tenant ON auth.service_clients (tenant_id);
ALTER TABLE auth.service_clients ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_service_clients_tenant ON auth.service_clients
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

CREATE TABLE auth.upstream_service_issuers (
  iss              TEXT PRIMARY KEY,                  -- e.g. https://token.actions.githubusercontent.com
  tenant_id        UUID NOT NULL,
  jwks_uri         TEXT NOT NULL,
  required_claims  JSONB NOT NULL DEFAULT '{}',       -- e.g. { "repository": "acme/agent-runtime" }
  allowed_audiences TEXT[] NOT NULL,
  allowed_scopes   TEXT[] NOT NULL,
  status           VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**API Endpoints:**
```
POST   /oauth/token                       OAuth2 token endpoint     [public, body-authenticated]
POST   /v1/admin/service-clients          Register service client   [scope: tenant:admin]
GET    /v1/admin/service-clients          List service clients      [scope: tenant:admin]
DELETE /v1/admin/service-clients/{id}     Revoke service client     [scope: tenant:admin]
POST   /v1/admin/service-clients/{id}/rotate-secret   Rotate secret [scope: tenant:admin]
POST   /v1/admin/upstream-service-issuers Register federated issuer [scope: tenant:admin]
```

**`/oauth/token` request shapes:**

```
# Mode A: static client_secret
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=<uuid>
&client_secret=<secret>
&audience=<target-service-name>     # e.g. "llms-gateway"
&scope=internal%3Aread+internal%3Awrite

# Mode B: federated OIDC (RFC 7521)
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<jwt from GitHub Actions / GCP IAM / AWS IAM>
&audience=<target-service-name>
&scope=internal%3Aread

# Response (both modes, same shape — RFC 6749)
HTTP/1.1 200 OK
Content-Type: application/json
{
  "access_token": "eyJ...",                  # the service JWT (Contract 12 shape)
  "token_type":   "Bearer",
  "expires_in":   3600,
  "scope":        "internal:read internal:write"
}
```

> **TTL clarification (2026-06):** EXTERNAL service-client tokens minted here have TTL ≤ 3600s (`expires_in` above). This is deliberately distinct from the **300s cap on INTERNAL service tokens** (Component 8b / Contract 12) — the 300s cap applies only to bootstrap-secret-minted in-cluster tokens, not to external `client_credentials` clients.

**Audit:** every issuance and every refusal logged to `auth.audit_log` (event_type=`oauth_token.issued` / `oauth_token.refused`).

---

### Component 8b-disc — OIDC Discovery Endpoint ⚡ (NEW — implements Contract 1 OIDC discovery)

A standard `/.well-known/openid-configuration` document so external SDKs can auto-configure.

```
GET /.well-known/openid-configuration

Response (RFC 8414):
{
  "issuer":                                "{AUTH_ISSUER_URL}",
  "jwks_uri":                              "{AUTH_ISSUER_URL}/.well-known/jwks.json",
  "token_endpoint":                        "{AUTH_ISSUER_URL}/oauth/token",
  "registration_endpoint":                 "{AUTH_ISSUER_URL}/v1/admin/service-clients",
  "scopes_supported":                      ["openid", "internal:read", "internal:write",
                                            "llm:invoke", "rag:query", "memory:read",
                                            "memory:write", "tool:invoke",
                                            "guardrails:check", "tenant:read",
                                            "tenant:admin", "platform:admin"],
  "response_types_supported":              ["token"],
  "grant_types_supported":                 ["client_credentials"],
  "token_endpoint_auth_methods_supported": ["client_secret_post",
                                            "private_key_jwt",
                                            "client_secret_jwt"],
  "subject_types_supported":               ["public"],
  "id_token_signing_alg_values_supported": ["RS256"],
  "claims_supported":                      ["sub", "iss", "aud", "exp", "iat", "jti",
                                            "tenant_id", "agent_id", "api_key_id",
                                            "scopes", "plan", "region", "deployment_id"],
  "code_challenge_methods_supported":      []   // PKCE not in v1 (no auth_code flow yet)
}
```

> This document is the single source of truth that lets every standard OIDC client library configure itself with one URL. SDKs MUST use it for endpoint discovery; hardcoded endpoint URLs in SDK code are forbidden.

> **Amended 2026-06:** `introspection_endpoint` and `revocation_endpoint` are intentionally ABSENT from the document until `/oauth/introspect` and `/oauth/revoke` actually ship — advertising endpoints that 404 breaks standard OIDC clients. The keys are re-added in the phase that ships those endpoints.

---

### Component 8c — Workload Identity Attestation 📋 (NEW — addresses Problems 2 & 4)

> **Why this matters:** Today, possession of credentials = trusted agent. Bootstrap secrets and even agent API keys can be exfiltrated and replayed from anywhere. For the long-running, high-blast-radius components of an agent platform (orchestrators, memory writers, payment-class tools), the issuer must verify *what is running, not just what knows the secret*.
>
> The plan below makes SPIFFE the canonical workload identity for everything inside the cluster and the binding target for `cnf` claims (Component 3b). External-developer agents fall back to OIDC-attested CI builds via Sigstore. The two together remove "secret-only" trust paths from production.

**SPIRE deployment (Phase 13):**

```
SPIRE Server (1 per cluster, stateful, HA-replicated):
  - Issues SVIDs (SPIFFE Verifiable Identity Documents — x509 certs)
  - Trust domain: spiffe://cypherx.<env>
  - Federates with peer trust domains (px0, partner orgs) for cross-platform A2A

SPIRE Agent (DaemonSet on every node):
  - Attests node identity via AWS EC2 IID + IAM role
  - Attests workload identity via K8s pod attestor (verifies pod uid, namespace, service account, image digest)
  - Issues SVID to workload through Workload API Unix socket

Workload SVID claim format:
  spiffe://cypherx.prod/ns/shared-core/sa/llms-gateway
  spiffe://cypherx.prod/ns/xagent/sa/agent-runtime/image-digest/sha256:abc...
```

**Auth integration:**

```
1. Service /v1/service-tokens flow gains a SPIFFE mode:
     Caller presents SVID (x509 mTLS handshake to Auth via Istio).
     Auth extracts spiffe:// URI from cert SAN.
     Looks up auth.service_acl WHERE caller_service = derived from SPIFFE path.
     Mints service JWT with cnf.x5t#S256 = sha256(presented SVID).
     Bootstrap-secret mode is rejected once SPIFFE is enabled per service.

2. Agent token endpoint gains a workload-bound mode (for in-cluster agents):
     Agent-runtime pod presents its SVID to Auth /v1/agents/{id}/token.
     Auth verifies the SVID's service account is whitelisted to run agent_id (auth.agent_workload_acl).
     Minted agent JWT carries cnf.x5t#S256 + wkl_id claim (the SPIFFE URI).

3. Every service verifies on inbound:
     If cnf.x5t#S256 present, sha256(X-Forwarded-Client-Cert) MUST match (Istio sidecar exposes cert).
     If wkl_id present, the trust-domain prefix MUST match expected env.
```

**Data model addition:**

```sql
CREATE TABLE auth.agent_workload_acl (
  agent_id          UUID NOT NULL REFERENCES auth.agents(agent_id),
  spiffe_id_prefix  VARCHAR(500) NOT NULL,
                    -- e.g. spiffe://cypherx.prod/ns/xagent/sa/agent-runtime/
                    -- Match is prefix-based so a single ACL row covers all image digests.
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (agent_id, spiffe_id_prefix)
);
```

**External-developer agents (post-SDK):**

```
External agents run on customer infrastructure → no SPIFFE attestor we control.
Acceptable attestation paths (declared at agent registration):
  - sigstore-keyless: agent presents a Sigstore-signed OIDC token (GitHub Actions, GitLab CI).
    Auth verifies signature against the Sigstore transparency log and the agent's declared
    OIDC issuer + repo + workflow.
  - api-key-only: explicit downgrade tier. Agent is flagged untrusted-external; cannot be
    granted approval-requiring scopes (Component 10), and is subject to a stricter default
    behavioral envelope (Component 5c).
```

**Mandatory cutover before Phase 13 ships:**

- Internal services: bootstrap-secret mode disabled in prod. Service A without an SVID cannot obtain a token.
- External developers: api-key tier remains, but new tenants default to sigstore-keyless required for `payments:*`, `infra:*`, and `data:bulk_delete` scopes (these come from Component 10's approval-required list).

---

### Component 8d — A2A Delegation Chain Validation 📋 (NEW — addresses Problem 6, auth side)

> **Why this matters:** The current `delegation_from / delegation_scope / delegation_task` claim set lets you express *one* delegation. The moment a chain forms (A → B → C → D) there is no architectural defence against scope creep, expiry extension, or unauthorised re-delegation. Each hop is a fresh token that the receiver trusts because Auth signed it — but Auth never sees the chain holistically.
>
> The fix is to encode the **entire chain** in every A2A token and have Auth refuse to mint a new link that violates parent constraints. The chain becomes its own authorisation evidence; receivers can verify the whole path without calling back to Auth.

**Augmented A2A token claims (alongside Contract 1 base claims):**

```json
{
  "delegation_chain": [
    {
      "from":       "<root-agent-id>",
      "to":         "<agent-B-id>",
      "task_id":    "<uuid>",
      "scopes":     ["llm:invoke"],
      "issued_at":  1716384000,
      "expires_at": 1716384300,
      "transitive": false,
      "kid":        "<auth-signing-kid>",
      "sig":        "<base64-RS256 over canonical-json of this entry>"
    },
    {
      "from":       "<agent-B-id>",
      "to":         "<agent-C-id>",
      "task_id":    "<uuid>",
      "scopes":     ["llm:invoke"],
      "issued_at":  1716384005,
      "expires_at": 1716384300,
      "transitive": false,
      "kid":        "<auth-signing-kid>",
      "sig":        "<base64-RS256 over canonical-json of this entry>"
    }
  ],
  "delegation_depth":       2,
  "delegation_root_task":   "<root-task-uuid>",
  "delegation_root_expiry": 1716384300
}
```

**Auth `/v1/agents/{id}/a2a-token` enforces (NEW rules):**

```
When agent B requests a delegated token to call agent C, B presents its parent token P.

Validation cascade (every check must pass; failure → 403):

  1. P.delegation_depth + 1 ≤ MAX_DELEGATION_DEPTH (default 5; configurable per tenant).
  2. requested_scopes ⊆ P.last_chain_entry.scopes (no scope escalation).
  3. requested_expires_at ≤ P.delegation_root_expiry (no expiry extension).
  4. P.last_chain_entry.transitive == true OR B == P.delegation_chain[0].from
     (only the root can default-delegate; intermediate agents may only delegate if
      explicitly allowed by parent's transitive flag).
  5. Cycle check: requested receiver C must not appear earlier in P.delegation_chain
     (rejects A→B→C→A). If C appears, return 403 DELEGATION_CYCLE.
  6. Each existing chain entry's `sig` verifies against the Auth signing key whose
     `kid` it cites (defence against forged chains presented by a compromised agent).

If all pass:
  - Append a new chain entry, sign it with Auth's current signing key.
  - delegation_depth = P.delegation_depth + 1.
  - delegation_root_task and delegation_root_expiry carried verbatim.
  - Mint and return.
```

**Receiver-side verification (every a2a-router, every agent endpoint):**

```
On inbound A2A request:
  1. Verify outer JWT signature (Contract 1).
  2. For each entry in delegation_chain:
       a. Verify entry.sig over canonical-json(entry minus sig) using JWKS[entry.kid].
       b. Verify entry.to == next entry's entry.from (chain continuity).
       c. Verify entry.expires_at ≤ delegation_root_expiry (no link extended root).
       d. Verify entry.scopes ⊆ previous entry.scopes (monotone non-increasing).
  3. Verify chain[-1].to == this agent's agent_id (token actually addressed to us).
  4. Verify NOW < delegation_root_expiry (root expiry overrides per-link).
  5. Verify request action ⊆ chain[-1].scopes.

Any failure → 401 DELEGATION_CHAIN_INVALID with a precise reason field for debugging.
```

**Why root_expiry, not just per-link expiry:**

- Per-link expiry alone allows a long chain to outlive the root's intent (root grants 5 min; B issues 5 min to C at minute 4; C issues 5 min to D at minute 9 — D's link is technically valid but D is now operating 9 minutes past root's intent).
- `delegation_root_expiry` is set once by the root and is the absolute ceiling. No re-issuance can extend it. This is the rule receivers actually enforce in step 4.

**Default `transitive` policy:**

- The root token issued by Auth defaults to `transitive: false`. Intermediate agents that try to re-delegate are rejected at step 4 of the cascade above.
- Setting `transitive: true` requires the root agent's owner to have the `delegation:transitive` scope (a new scope, added to the default platform policy as denied-by-default). For workflows that legitimately need deep chains (orchestrators that fan out 3+ levels), this scope is granted explicitly.

**Cycle detection — runtime + log:**

- Step 5 above catches direct cycles (C appearing earlier in chain).
- For *indirect* cycles via async A2A (A → B publishes Kafka → eventually triggers A), the chain doesn't help. Mitigation: every `cypherx.agent.a2a.delegated` event carries `delegation_root_task`; the orchestrator/a2a-router maintains a per-root_task set in Valkey and rejects if cardinality > 50 unique agents (configurable). Logged as `cypherx.agent.a2a.cycle_suspected`.

---

### Component 9 — ABAC Extension 📋

**What it is:** Attribute-Based Access Control — adds conditional rules beyond simple scope checks.

```json
Example ABAC rule:
{
  "action": "llm:invoke",
  "effect": "allow",
  "conditions": [
    { "attribute": "plan", "operator": "in", "values": ["pro", "enterprise"] },
    { "attribute": "model", "operator": "not_in", "values": ["gpt-4-turbo"] }
  ]
}
```

---

### Component 9b — Policy Compilation & Distributed Decision Path 📋 (NEW — addresses Problem 3)

> **Why this matters:** Every `/authorize` call is a network round-trip + DB lookup + ABAC evaluation + audit write. At low volume, the Valkey 30s cache absorbs the cost. Under agent swarms — an orchestrator fanning to 50 specialists, each making 20 tool calls — `/authorize` becomes the synchronous bottleneck and the audit-write becomes a write-amplified hot path on `auth.audit_log`. The fix is to compile policy into a distributable bundle so decisions are made in-process where the action originates.
>
> The path below is the *forward* design. First cycle continues to use the existing endpoint; the migration is dual-write so Auth retains source-of-truth control until parity is proven.

**Compilation target: OPA (Open Policy Agent) Rego bundles.**

Alternatives considered: Cedar (newer, less tooling), Zanzibar-style (relationship-graph; overkill for our flat-RBAC + ABAC shape), homegrown DAG (rejected — not a differentiator). OPA chosen for maturity, sidecar story, and existing K8s integration.

**Architecture:**

```
Auth service (source of truth)
  │
  ├── On policy change (auth.policies / auth.behavior_policies / auth.service_acl write):
  │     1. Bundle builder compiles current state to Rego: policies → rules; behavior →
  │        a JSON data document loaded alongside the Rego module.
  │     2. Bundle uploaded to S3 bucket cypherx-policy-bundles/<env>/<sha>.tar.gz
  │     3. Auth publishes cypherx.auth.policy.bundle.published with bundle SHA.
  │
  └── OPA sidecar (deployed alongside every service):
        - Polls bundle endpoint every 30s (or pub/sub via Kafka consumer).
        - Holds compiled bundle in memory; evaluates decisions locally.
        - Decision logs streamed to Loki + sampled to auth.audit_log via Kafka topic
          cypherx.auth.decision.streamed (1% sample for non-deny; 100% sample for deny).

Service handler (instead of POST /v1/authorize):
  result = opa.evaluate("data.cypherx.allow", { agent_id, tenant_id, action, resource })
  if result.allowed: proceed
  else: 403 with result.reason

Auth /v1/authorize remains as the slow-path fallback:
  - If OPA sidecar reports `unknown` (rule not yet covered) → fall through to /v1/authorize.
  - If OPA bundle is older than 60s and Auth has published a newer one → service forces
    a refresh before answering.
```

**Why dual-write during migration:**

```
For the first 30 days after OPA rollout, every service evaluates BOTH:
  decision_opa    = opa.evaluate(...)
  decision_remote = POST /v1/authorize (same input)

If decision_opa != decision_remote:
  - Log cypherx.auth.decision.divergence with both decisions + input.
  - Honour decision_remote (Auth is source of truth).
  - Alert on > 0.1% divergence rate.

When divergence rate stays < 0.01% for 7 consecutive days, services flip a feature flag
to OPA-only. Auth /v1/authorize is kept available indefinitely for cases where OPA cannot
answer (e.g., stateful tenant-suspension check).
```

**Decision-class split (what runs where):**

| Decision class | Where it runs | Why |
|----------------|---------------|-----|
| Scope membership (`scope ∈ jwt.scopes`) | Always local (Layer A, no OPA needed) | Pure JWT inspection |
| RBAC policy match (tenant_plan + action) | OPA sidecar | Compilable, no DB read |
| ABAC attribute check (e.g., `agent.region == "us-east"`) | OPA sidecar (attrs in bundle data) | Compilable if attrs are in bundle |
| Behavioral envelope (rate, sequence) | Component 5c middleware (Valkey counters) | Stateful, cannot be bundled |
| Tenant suspension status | Auth `/authorize` (slow path) | Source of truth must be DB; small fraction of calls |
| Step-up approval check (Component 10) | Auth `/authorize` | Approval tokens age out fast; need fresh DB read |

**Audit guarantees preserved:**

- OPA decision-log stream → Kafka `cypherx.auth.decision.streamed` → `auth.audit_log` consumer.
- Deny decisions sampled at 100% (always logged).
- Allow decisions sampled at 1% by default, configurable per tenant for compliance-heavy customers (HIPAA tenants → 100% allow logging).
- The dropped 99% of allow-decisions are still captured in OPA's local decision log files on the sidecar (24h ring buffer), exfiltrable on incident.

**First-cycle stance:**

- ⚡ first cycle: KEEP the in-DB policy + Valkey-cached `/authorize` design (Component 4). Do NOT ship OPA in first cycle — it's a real architecture migration and the existing design is sufficient until the first multi-agent orchestration workload lands.
- 📋 Phase 13 / pre-scale: ship Auth bundle builder + service-side OPA sidecars + 30-day dual-write + cutover.

---

### Component 10 — Human Approval & Step-Up Authorization 📋 (NEW — addresses Problem 7)

> **Why this matters:** The platform currently treats autonomous and human-initiated actions identically — if an agent has `payments:execute` in scope, it executes. For irreversible or high-blast-radius actions (payments, bulk delete, infra writes, external-API mutations, sub-agent spawning), the architecture must support *fresh human approval* as a precondition, separate from the agent's standing scopes.
>
> The Phase 10 human-in-the-loop workflow pause covers UX-level approval gates for *workflows*. This component is the **authorisation layer** version: an approval is a signed, scoped, short-lived assertion that a specific user authorised a specific agent to do a specific action at a specific moment.

**Approval-required scopes (default platform list, tenant-overridable):**

| Scope | Why it requires approval |
|-------|-------------------------|
| `payments:execute` | Irreversible money movement |
| `data:bulk_delete` | Mass deletion across tenant data |
| `infra:write` | Infra-mutating tool calls (deploy, scale, terminate) |
| `external_api:write` | Mutating calls to third-party APIs outside the platform |
| `agent:create_subagent` | Spawning new agents (containment for runaway recursion) |
| `policy:write` | Tenant policy modifications (escalation risk) |

Tenants register custom approval-required scopes via `POST /v1/admin/approval-policy`.

**Data model (PostgreSQL — `auth.approval_requests` + `auth.approval_grants`):**

```sql
CREATE TABLE auth.approval_requests (
  request_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL,
  agent_id      UUID NOT NULL,
  task_id       UUID,                          -- bind approval to specific task
  scopes        TEXT[] NOT NULL,               -- scopes being requested
  resource      VARCHAR(500),                  -- e.g. "payment:invoice-123"
  reason        TEXT,                          -- agent's stated reason (shown to user)
  context       JSONB,                         -- structured context for UI rendering
  status        VARCHAR(20) NOT NULL DEFAULT 'pending',
                -- pending | granted | denied | expired
  requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ NOT NULL,          -- request itself times out after 5 min default
  resolved_at   TIMESTAMPTZ,
  resolved_by   UUID,                          -- user_id from px0
  resolution_note TEXT
);

CREATE INDEX idx_approval_pending ON auth.approval_requests(tenant_id, status, expires_at)
  WHERE status = 'pending';

CREATE TABLE auth.approval_grants (
  grant_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id    UUID NOT NULL REFERENCES auth.approval_requests(request_id),
  tenant_id     UUID NOT NULL,
  agent_id      UUID NOT NULL,
  approved_by   UUID NOT NULL,                 -- user_id from px0
  scopes        TEXT[] NOT NULL,               -- subset granted (may be < requested)
  resource      VARCHAR(500),
  task_id       UUID,
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ NOT NULL,          -- grant TTL: 15 min default, never > 1h
  consumed_at   TIMESTAMPTZ,                   -- one-shot grants flip this on first use
  one_shot      BOOLEAN NOT NULL DEFAULT TRUE,
  step_up_method VARCHAR(50)                   -- mfa | webauthn | password | sso-reauth
);

CREATE INDEX idx_grants_active ON auth.approval_grants(agent_id, task_id) WHERE consumed_at IS NULL;
```

**Approval token (Contract 16 — see Phase 0):**

```json
{
  "iss":           "https://auth.cypherx.ai",
  "sub":           "<agent_id>",
  "aud":           ["cypherx-platform"],
  "iat":           1716384000,
  "exp":           1716384900,
  "jti":           "<uuid>",

  "tenant_id":     "<org-uuid>",
  "agent_id":      "<agent-uuid>",
  "grant_id":      "<grant-uuid>",
  "approved_by":   "<user-uuid-from-px0>",
  "approval_scopes": ["payments:execute"],
  "approval_resource": "payment:invoice-123",
  "approval_task": "<task-uuid>",
  "step_up_method": "webauthn",
  "one_shot":      true
}
```

This is a *separate* token from the agent JWT — minted by Auth only after a user step-up flow on the frontend (or px0's identity surface) succeeds. The agent presents both tokens on the action call.

**Flow:**

```
1. Agent attempts a protected action (e.g., POST /v1/tools/tool-payments/invoke).
2. Tool server's auth middleware sees `payments:execute` is approval-required.
3. Middleware checks for `X-Approval-Token` header.
   If absent or invalid → 401 APPROVAL_REQUIRED with body:
     { "approval_required": true,
       "request_endpoint": "https://auth.cypherx.ai/v1/approvals/request",
       "scopes": ["payments:execute"],
       "task_id": "<uuid>" }

4. Agent POSTs to /v1/approvals/request with scopes + task_id + reason + context.
   Auth creates auth.approval_requests row, publishes cypherx.auth.approval.requested.
   Returns: { "request_id": "<uuid>", "expires_at": "..." }

5. Frontend (subscribed to Kafka or polling) shows approval prompt to user.
   User clicks Approve → frontend invokes px0 step-up flow (re-auth / MFA / WebAuthn).
   On success, frontend POSTs to /v1/approvals/{request_id}/grant with the step-up assertion.

6. Auth validates step-up assertion against px0 JWKS.
   Creates auth.approval_grants row.
   Mints approval token (Contract 16).
   Returns approval token to frontend → forwards to waiting agent (via task callback / SSE).

7. Agent retries the action with X-Approval-Token: <Bearer>.
   Tool server verifies:
     a. Token signature, exp, tenant match.
     b. approval_scopes ⊇ required scope for this action.
     c. approval_resource matches the resource being acted on (or wildcard).
     d. approval_task matches the current task_id.
     e. one_shot flag → atomic check-and-set on auth.approval_grants.consumed_at.
        If already consumed → 401 APPROVAL_EXHAUSTED.
   On all pass → execute the action.
```

**Hard rules:**

- Approval grants are **bound to a (agent_id, task_id, resource) tuple** by default. Same agent, different task, different resource → fresh approval required.
- Approval grants are **one-shot by default** (`consumed_at` flips on first use). Multi-shot grants (e.g., "approve all payment writes for this workflow") require explicit `one_shot: false` at grant time and have a maximum 1h TTL.
- Approval cannot be granted by the agent owner if that owner *is* the agent — must be a distinct user identity. (Prevents self-approval on hijacked credentials.)
- Step-up method must satisfy the tenant's policy: `payments:*` may require `webauthn` or `mfa`; `infra:write` may accept `sso-reauth`. Policy in `auth.tenant_step_up_policy` — this table's DDL DEFERS with Component 10 (📋, no first-cycle migration; see Amendment Log).

**Audit:**

- Every approval request, grant, and consumption is written to `auth.audit_log` with `event_type ∈ {approval.requested, approval.granted, approval.denied, approval.consumed, approval.expired}`.
- `cypherx.auth.approval.granted` Kafka event surfaces in the compliance pipeline.

**Why this is NOT just RBAC + a flag:**

- RBAC says "this agent *may* perform payments." Approval says "this agent *was authorised* to perform *this* payment at *this* moment by *this* user." The two layers compose: scope is a precondition, approval is the in-the-moment authority.
- Approval grants are revocable instantly (user clicks "cancel approval" → row marked consumed_at + denied). Revocation propagates via Valkey grant cache invalidation.

**First-cycle stance:**

- ⚡ first cycle: ship the table schema and the `approval_required` flag on policies, but mark all default scopes as `approval_required: false`. No approval flow active. This bakes the data model in early so retrofitting Phase 10 workflows is cheap.
- 📋 Phase 9C onward (when human-in-the-loop workflow lands): wire the full request/grant/consume flow and connect to px0 step-up.

---

### Component 11 — Upstream Identity (px0) Verification ⚡ Config-gated (NEW)

> **Config gate (amended 2026-06):** verification is driven entirely by rows in `auth.upstream_identity`. First cycle ships the TABLE EMPTY — an **empty table means upstream (px0) verification is disabled**, and endpoints that would consume `X-Px0-User-Token` fall back to their non-px0 auth paths (bootstrap token / `platform:admin` agent JWT). This matches current code behavior, which is correct. The px0 seed row (issuer, JWKS URL, pinned root) moves to Phase 11 with the px0 bridge — px0 is not provisionable in the compose runtime.

> **Why this matters:** px0 owns *user* identity. CypherX AI owns *agent* identity. Component 10 (step-up approval) and Component 1 (`created_by: user_id from px0`) both reference px0 user identities, but until now this doc never specified **how Auth actually verifies a px0 JWT**. Without it: the approval flow has no anchor for "this is really user X", and any bug in the bridge would silently accept forged user identities.
>
> This component nails down the verification protocol once, so every other component referencing `user_id from px0` has a precise meaning.

**px0 JWKS configuration (active only once a px0 row is seeded — Phase 11):**

```
Auth maintains a configured trust anchor for px0:
  px0_issuer          = "https://identity.px0.cypherx.ai"   (env-specific)
  px0_jwks_url        = "https://identity.px0.cypherx.ai/.well-known/jwks.json"
  px0_audience        = "cypherx-platform"     (the audience px0 mints for us)
  px0_signed_bundle   = "https://identity.px0.cypherx.ai/.well-known/jwks-signed.json"

Auth fetches px0_signed_bundle every 6h, verifies its signature against a pinned px0
root JWK shipped in Auth's config (Doppler path: trust/px0_root_jwk_pem). On signature
failure: alert and continue using last-known-good cache (do NOT auto-trust the unsigned
JWKS fallback — that defeats the protection).

Config table (PostgreSQL — auth.upstream_identity):
  CREATE TABLE auth.upstream_identity (
    issuer          VARCHAR(500) PRIMARY KEY,         -- "px0" for now; future federated IdPs
    jwks_url        VARCHAR(500) NOT NULL,
    audience        VARCHAR(255) NOT NULL,
    root_jwk_pem    BYTEA NOT NULL,                   -- pinned root for signed-bundle verify
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );
```

**Verification flow (every place that consumes a px0 JWT — Component 10 approval, Component 1 bootstrap, admin tenant ops):**

```
Given: incoming px0 JWT in X-Px0-User-Token header (NOT in Authorization — that slot
       is reserved for the agent JWT or service JWT).

1. Parse JWT header → extract kid.
2. Resolve kid against in-process px0 JWKS cache (5-min TTL, fed by signed bundle
   fetch). On miss: refresh-from-bundle, max 1/min.
3. Verify signature with the resolved key.
4. Verify standard claims:
     iss == "https://identity.px0.cypherx.ai"  (literal match, not regex)
     aud contains "cypherx-platform"
     exp in future, nbf in past, ±60s skew tolerance
     iat ≥ NOW() - 1h  (px0 user JWTs MUST be fresh; refuse stale tokens for safety)
5. Verify required claims present:
     sub          → maps to user_id (also stored in claim user_id for clarity)
     org_id       → MUST equal the tenant_id of the call context (no cross-tenant)
     auth_method  → mfa | webauthn | password | sso  (required for step-up; Component 10
                    rejects passwords-only for payments-class actions)
     auth_time    → seconds-since-epoch of the user's last authentication event.
                    For step-up approval, MUST be within last 15 min.
6. (Optional) Verify `acr` (Authentication Context Class Reference) for risk-based policies.

On any failure: 401 PX0_TOKEN_INVALID with reason field.
On success: the call has a verified px0 user identity. Audit row written with
            event_type = px0.identity.verified.
```

**Claim mapping (px0 → CypherX):**

| px0 claim | CypherX usage |
|-----------|---------------|
| `sub` | `auth.audit_log.revoked_by`, `auth.approval_grants.approved_by`, `auth.agents.created_by` |
| `org_id` | MUST match `tenant_id` in the request context |
| `auth_method` | `auth.approval_grants.step_up_method` |
| `auth_time` | Freshness check for Component 10 step-up approvals |
| `email`, `name` | Logging only (never stored in CypherX DB — px0 is source of truth) |

**Failure scenarios — explicit handling:**

| Scenario | Behaviour |
|----------|-----------|
| px0 JWKS unreachable for > 6h | Auth refuses to verify new px0 tokens; in-flight platform ops continue with last-cached JWKS for up to 1h (then start failing); incident escalation |
| px0 root key compromised | Out-of-band: rotate pinned root in `auth.upstream_identity.root_jwk_pem` (admin-only, with two-person approval); restart Auth pods |
| px0 issues a token with `org_id` we don't know about | 401 `TENANT_UNKNOWN`; px0 should publish the `px0.org.created` event before users authenticate (Contract 13 lifecycle) |
| User's `auth_time` is older than approval freshness window | 401 `STEP_UP_REQUIRED`; client must re-auth on px0 |

**API endpoints (admin only):**

```
GET    /v1/admin/upstream-identity            List configured upstream IdPs
PATCH  /v1/admin/upstream-identity/{issuer}   Update JWKS URL / root pin (two-person)
POST   /v1/admin/upstream-identity/refresh    Force JWKS bundle refresh (incident response)
```

**Why this is its own component, not buried inside Component 10:**

- Multiple components depend on px0 verification (10, 1, admin tenant ops). Centralising it makes the trust boundary explicit.
- Federated identity (future enterprise SSO via OIDC providers other than px0) plugs into the same `auth.upstream_identity` table — no schema change when adding Okta / Azure AD.
- Auditors look for a single document describing the trust path from "user clicked Approve" to "agent executes payment". This component is that document.

**First-cycle stance (amended 2026-06):**

- ⚡ first cycle: ship the `auth.upstream_identity` TABLE ONLY — no seed row. With the table empty, px0 verification is disabled and `X-Px0-User-Token` is not accepted anywhere; bootstrap and admin tenant endpoints use the bootstrap token / `platform:admin` JWT paths. The verification flow, claim mapping, and admin endpoints above are the design that activates when a row exists.
- 📋 Phase 11: seed the px0 row (issuer, JWKS URL, pinned root) with the px0 bridge; signed-bundle verification + claim mapping go live for bootstrap, admin tenant ops, and (later) Component 10 approval grants. (moved to Phase 11 — see Amendment Log)
- 📋 Phase 13: federated IdPs (Okta / Azure AD); risk-based `acr` policies; user-revocation propagation from px0 (Kafka `px0.user.deactivated` → cascade to `auth.audit_log` + invalidate any approval grants from that user).

---

### Integration Contracts (How Other Services Use Auth)

**Two-layer model — local verify first, remote authorize only when needed:**

Calling `/authorize` on every request would gate every API call behind a network round-trip. Most checks are stateless (scope membership, JWT validity) and can be made **locally** by every service using the cached JWKS. `/authorize` is reserved for **stateful** decisions (policy evaluation, tenant suspension status, ABAC attribute checks) that require the Auth database.

```
Every request, every service:
  Layer A — LOCAL (always, no network):
    1. Verify JWT signature against cached JWKS (Kong did this at edge — services re-verify
       as defence-in-depth in case of internal call without going through Kong).
    2. Verify exp/nbf/iss/aud claims.
    3. Verify required scope is in the JWT's scopes claim.
    4. Extract tenant_id, agent_id from claims.
    On any failure → 401 UNAUTHORIZED.

  Layer B — REMOTE (only if action requires stateful authz):
    1. POST /v1/authorize { agent_id, tenant_id, action, resource, context }
    2. Auth returns allow/deny based on RBAC policy + ABAC + tenant status.
    3. Response cached in caller's local cache, key = sha256(agent_id|action|resource), TTL 30s.
    On deny → 403 FORBIDDEN with reason from response.
```

**Rule of thumb:** if the action is bounded by scope only (e.g., "agent has llm:invoke scope, allow the LLM call") use Layer A only. If the action depends on something Auth knows but the caller doesn't (e.g., "tenant exceeded plan limit", "policy was updated to deny gpt-4-turbo for free-tier"), Layer B is required.

**Inter-service authentication:**
- Service-to-service calls (e.g., xAgent → Guardrails) carry **two** tokens:
  - `Authorization: Bearer <service-jwt>` — caller's identity (Contract 12). Used by the receiving service's Istio `AuthorizationPolicy` and by Auth `/authorize` to attribute the call.
  - `X-Forwarded-Agent-JWT: <agent-jwt>` — the original agent's identity. Used by the receiving service to enforce tenant/scope on behalf of the agent.
- Trace headers `traceparent`, `X-Request-ID`, `X-Tenant-ID`, `X-Agent-ID` are forwarded on every hop.

**Kong JWT Plugin config:**
```yaml
# Applied globally at Kong level
plugins:
  - name: jwt
    config:
      key_claim_name: kid
      claims_to_verify: [exp, nbf]
      uri_param_names: []
      header_names: [Authorization]
      secret_is_base64: false
      # JWKS URL — Kong fetches and caches public keys
      # NOTE: .well-known is at the origin root, NOT under /v1 (RFC 8615).
      jwks_uri: http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json
```

---

### Kafka Events Published by Auth

```
⚡ FIRST CYCLE:
  cypherx.auth.agent.registered
    payload (Contract 5 envelope wraps this):
      { agent_id, tenant_id, name, version, created_by, created_at, plan }

  cypherx.auth.agent.updated          ← required for /authorize L2 cache invalidation
    payload: { agent_id, tenant_id, change_type, updated_at }

  cypherx.auth.policy.changed         ← required for /authorize L2 cache invalidation
    payload: { policy_id, tenant_id, change_type, updated_at }

📋 POST FIRST CYCLE:
  cypherx.auth.agent.deactivated
    payload: { agent_id, tenant_id, reason }

  cypherx.auth.credential.rotated
    payload: { agent_id, tenant_id, old_key_id, new_key_id }

  cypherx.auth.key.revoked
    payload: { agent_id, tenant_id, key_id, reason }

  cypherx.auth.service_token.issued   ← high-volume; opt-in for audit pipelines
    payload: { caller_service, on_behalf_of, tenant_id, issued_at, ttl }
```

**Transactional outbox + relay (added 2026-06 — event fidelity):**

Provisioning-critical events were previously published best-effort (log-and-drop on broker failure), which violates the ≤5s staleness SLA (Audit Addendum #6) and can silently desynchronise every downstream service. Auth now ships a transactional outbox + relay — the same pattern as the other three services:

```sql
CREATE TABLE auth.outbox (
  id            BIGSERIAL PRIMARY KEY,
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(200) NOT NULL,        -- tenant_id for tenant-scoped events
  payload       JSONB NOT NULL,               -- Contract 5 envelope
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ                   -- NULL until the relay publishes
);
CREATE INDEX ix_auth_outbox_unpublished ON auth.outbox (id) WHERE published_at IS NULL;
```

```
Covered topics — the outbox row is written in the SAME transaction as the state change;
the relay (in-service background loop) polls unpublished rows, publishes to Kafka, and
marks published_at (at-least-once; consumers de-duplicate on event id):

  cypherx.tenant.*                 created | suspended | resumed | plan_changed |
                                   pending_deletion | deleted   (Contract 13 backbone)
  cypherx.auth.token.revoked       Component 3c
  cypherx.auth.policy.changed      /authorize cache invalidation

Direct-publish (best-effort) remains acceptable for the advisory topics only:
  cypherx.auth.agent.registered / agent.updated and the 📋 topics above — they are not
  provisioning-critical; consumers self-heal via TTL'd caches.
```

---

### K8s Deployment Spec

```yaml
Namespace:   shared-core
Deployment:  auth-service
Replicas:    min 2, max 8 (HPA on CPU 70%)
Node selector: node-role: core

Resources:
  requests: { cpu: 200m, memory: 256Mi }
  limits:   { cpu: 1000m, memory: 512Mi }

Health probes (per Contract 7 — post-edit):
  livenessProbe:  GET /livez  (initialDelay: 10s, period: 10s)
                  Process-only check; NEVER touches DB/Valkey/Kafka/KMS.
  readinessProbe: GET /readyz (initialDelay: 5s, period: 5s)
                  Hard dependencies (fail readiness): PostgreSQL connectivity,
                    KMS Decrypt of signing key on startup (cached after first success).
                  Soft dependencies (log warning only): Valkey, Kafka.

Env vars (from Doppler → K8s Secret):
  DATABASE_URL              (PgBouncer connection string, auth_user / auth schema)
  VALKEY_URL                (Valkey connection string — soft dependency)
  KAFKA_BROKERS             (MSK broker addresses)
  KAFKA_SASL_PASSWORD       (MSK SASL credential)
  KMS_SIGNING_CMK_ARN       (KMS CMK alias for signing-key envelope encryption)
  ENVIRONMENT               (dev | staging | prod — gates CI-only tenant access)
  # NO JWT_PRIVATE_KEY, NO JWT_PUBLIC_KEY — signing keys live in auth.signing_keys,
  # encrypted with the KMS CMK above. Single source of truth.

Mounted secrets (NOT env vars — read on demand):
  Doppler path bootstrap/super_admin_token → file at /var/run/secrets/auth/bootstrap_token
    (consulted only when auth.agents is empty; ignored after bootstrap_state sentinel exists)
```

---

### Istio Authorization Policy

```yaml
# Who can call the auth service
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: auth-service-allow
  namespace: shared-core
spec:
  selector:
    matchLabels:
      app: auth-service
  action: ALLOW
  rules:
    - from:
        - source:
            namespaces: [shared-core, xagent, tools, platform-mgmt, ingress, px0-bridge]
      to:
        - operation:
            methods: [GET, POST, PATCH, DELETE]
```

---

## ⚡ First Cycle Implementation Checklist

- [ ] Service architecture planned separately (language, framework, internal structure)
- [ ] **Bootstrap super-admin path** (`POST /v1/admin/bootstrap` + `auth.bootstrap_state` sentinel)
- [ ] **Tenant admin endpoints** (`POST /v1/admin/tenants`, `GET`, suspend/resume, soft-delete) — `source` field accepts `px0-bridge | external-admin | self-serve-signup | sso-jit | manual-seed` per Contract 13
- [ ] **External Onboarding endpoints (Component 1c — first-cycle slice)** — `/v1/onboarding/signup`, `/verify`, `/resend` with config-driven SMTP emitter (MailHog/mock locally), pluggable captcha (mock provider first cycle), velocity-only risk scoring; on verify: tenant + first agent + API key — NO `tenant_users`, NO session JWT (Contract 20). `/upgrade` + `/close-account` (moved to Phase 11 — see Amendment Log)
- [ ] **Per-Tenant Quotas (Component 1d)** — `auth.plan_defaults`, `auth.tenant_quotas` tables + `GET/PUT /v1/admin/tenants/{id}/quotas`, `GET /v1/quotas` (Contract 19); `GET /v1/usage` backed by the `cypherx.llms.usage.recorded` consumer + `auth.tenant_usage_counters` rollup (WP04)
- [ ] **Webhook subscriptions (Component 1e — auth-owned)** — `auth.webhook_subscriptions` + `auth.webhook_deliveries` tables + CRUD + `auth-webhook-delivery` worker as its own compose service, signed POSTs + retry schedule per Contract 21 (referenced for signing/retry only)
- [ ] **Auth transactional outbox + relay** — `auth.outbox` table + relay loop publishing `cypherx.tenant.*`, `cypherx.auth.token.revoked`, `cypherx.auth.policy.changed`; outbox row written in the same transaction as the state change (no log-and-drop for provisioning-critical events)
- [ ] **Tenant lifecycle events emitted via the outbox** — `cypherx.tenant.created | suspended | resumed | plan_changed | pending_deletion | deleted` on every source (Contract 13)
- [ ] **Well-known tenants seeded** (`platform` + `integration-test`; CI tenant rejected in prod)
- [ ] Agent registration endpoint (`POST /v1/agents`)
- [ ] Agent detail endpoint (`GET /v1/agents/{agent_id}`)
- [ ] API key issuance (`POST /v1/agents/{agent_id}/keys`)
- [ ] API key revocation (`DELETE /v1/agents/{agent_id}/keys/{key_id}`)
- [ ] JWT minting (`POST /v1/agents/{agent_id}/token`) — header `kid` set, effective scopes = key ∩ agent ∩ requested
- [ ] **JWKS endpoint at origin root** (`GET /.well-known/jwks.json` — no /v1 prefix, RFC 8615)
- [ ] **Signing keys stored only in `auth.signing_keys`** (envelope-encrypted PEMs); NO `JWT_PRIVATE_KEY` env var
- [ ] Signing-key envelope encryption under an env-supplied KEK (`AUTH_SIGNING_KEK`, AES-256-GCM) — compose-parity form; the cloud form is the AWS KMS CMK `alias/cypherx-auth-signing-<env>` + IRSA grants kms:Decrypt/Encrypt/GenerateDataKey (infra phase)
- [ ] Signing key statuses: `signing | verifying | retired` + partial unique index `WHERE status='signing'`
- [ ] JWKS rotation runbook implemented and rehearsed on dev
- [ ] Authorization endpoint (`POST /v1/authorize`) — tenant_id from `X-Forwarded-Agent-JWT`, rejects body tenant_id
- [ ] Authorize cache keys include `tenant_id` + `hash(context)`; L2 invalidation via `policy.changed` / `agent.updated` events
- [ ] **Service-token endpoint (`POST /v1/service-tokens`)** — bootstrap-secret ONLY (no SPIFFE), ttl ≤ 300s, `aud=["*"]`, scopes derived server-side from `service_acl`
- [ ] **Service ACL seed migration** with the Component 8b 5-edge table (xagent→auth-service, xagent→llms-gateway, xagent→guardrails-service, llms-gateway→auth-service, guardrails-service→auth-service); additional edges land with their owning phases (aligned 2026-06 — see Amendment Log)
- [ ] **OAuth2 client_credentials endpoint (Component 8b-ext)** — `POST /oauth/token` with `client_secret_post` AND `private_key_jwt` (federated OIDC); `auth.service_clients` + `auth.upstream_service_issuers` tables; per-tenant client management endpoints (Contract 12 Mode 3)
- [ ] **OIDC discovery endpoint (Component 8b-disc)** — `GET /.well-known/openid-configuration` (Contract 1 OIDC discovery)
- [ ] **Audit log read API** — `GET /v1/audit-log`, `/verify`, `/export` (SOC 2 / HIPAA / GDPR table-stakes)
- [ ] Audit log writes on every auth decision and every token issuance
- [ ] **Secret redaction policy** enforced in logger middleware + CI test
- [ ] **`/livez`, `/readyz`, `/metrics`** endpoints — readiness gated on PostgreSQL + signing-key load (env-KEK decrypt; KMS in cloud form) only (Valkey/Kafka soft)
- [ ] Kafka events: `agent.registered`, `agent.updated` published on appropriate triggers; `policy.changed` via the outbox relay
- [ ] PostgreSQL schema migrations via **Atlas** (Contract 14) — `auth.tenants`, `auth.agents`, `auth.api_keys`, `auth.policies`, `auth.audit_log` (with `row_hash` + `prev_row_hash` + UPDATE/DELETE revoked from runtime role), `auth.signing_keys`, `auth.service_acl`, `auth.bootstrap_state`, `auth.behavior_policies` (table + ONE shadow seed row ONLY — middleware Phase 10, per Component 5c staging), `auth.approval_requests` + `auth.approval_grants` (schema only, Component 10 flow off in first-cycle), `auth.revoked_tokens` (Component 3c), `auth.upstream_identity` (Component 11 — table only, shipped EMPTY; px0 seed → Phase 11), `auth.rate_limit_config` (DDL + platform-default seed, Component 4), `auth.tenant_usage_counters` (Component 1d), `auth.webhook_subscriptions` + `auth.webhook_deliveries` (Component 1e), `auth.outbox`
- (`jti` replay-window middleware item deleted 2026-06 — single-use `jti` rescoped to one-time credentials only, Phase 13; bearer kill-switch = Component 3c revocation. See Amendment Log)
- [ ] **Verifiers accept-with-or-without `cnf`, `wkl_id`, `behavior_policy_id`, `delegation_chain`, `approval_context`** optional claims (Contract 1) — forward-compatibility
- [ ] **Component 3c — Live token revocation** — `POST /v1/tokens/revoke`, `POST /v1/agents/{id}/revoke-all-tokens`, Valkey `jti-revoked:{jti}` + bloom filter, Kafka `cypherx.auth.token.revoked` topic, kid-poisoned check
- [ ] **JWKS poisoning protection (compose-parity)** — in-stack JWKS fetches pinned to the env-configured internal Auth URL on the trusted compose network (cloud form: Istio DestinationRule + STRICT mTLS, infra phase); `/.well-known/jwks-signed.json` endpoint (signed bundle for external SDK clients) — signed bundle endpoint can be ⚡ stub returning 503 if SDK isn't shipping yet
- [ ] **Component 4 self-protection rate limits** — Valkey-backed per-endpoint quotas on `/v1/authorize`, `/v1/agents/{id}/token`, `/v1/service-tokens`, `/v1/admin/*`, `/v1/onboarding/*`; limits loaded from `auth.rate_limit_config` (DDL + seed); fail-open on Valkey outage with 50k rpm hard ceiling
- [ ] **Audit log tamper-evidence** — `row_hash` + `prev_row_hash` chain per tenant; UPDATE/DELETE grants revoked from runtime role; CI verification job; Kafka `cypherx.platform.audit.event` consumer writing Parquet to the S3-compatible store (MinIO locally, env-driven endpoint; cloud form: S3 with Object Lock, 90-day default)
- [ ] **Component 11 — px0 JWT verification (config-gated)** — `auth.upstream_identity` table shipped EMPTY (px0 seed moved to Phase 11 — see Amendment Log); empty table = upstream verification disabled and `X-Px0-User-Token` not accepted; verification path activates only when a row is seeded
- [ ] **Table-scope annotations honoured** — RLS on tenant-scoped tables only; platform-scoped tables documented in migration comments
- [ ] Services verify JWTs locally against `/.well-known/jwks.json` (no /v1 prefix) — no gateway in the compose runtime (cloud form: Kong JWT plugin pointed at the JWKS URL, infra phase)
- [ ] Runs as `auth-service` (+ `auth-webhook-delivery` worker) in the local compose stack — Neon Postgres + Valkey + Redpanda + MinIO, secrets via env file (cloud forms: K8s shared-core namespace via ArgoCD, Istio mTLS + AuthorizationPolicy, Doppler sync — infra phase)

## 📋 Full Enterprise Implementation Checklist

- [ ] Agent listing endpoint with pagination
- [ ] Agent update/patch endpoint
- [ ] Agent deactivation
- [ ] API key listing endpoint
- [ ] Key rotation (`POST /v1/agents/{agent_id}/keys/{key_id}/rotate`) with 24h grace window
- [ ] ABAC policy engine (condition-based rules)
- [ ] Policy management API (`GET /v1/policies`, `POST`, `PUT`, `DELETE`)
- [ ] A2A delegation token endpoint
- [ ] Service-to-service auth migrated from bootstrap-secret to **K8s TokenReview + SPIFFE** (Phase 13 hardening; per-target `aud` scoping)
- [ ] Auth decision caching in Valkey (30s TTL)
- [ ] Agent capability cache in Valkey (5min TTL)
- [ ] Kafka events: agent.deactivated, credential.rotated, key.revoked, service_token.issued
- [ ] Metrics: auth decisions/sec, JWT issuance rate, auth latency p99
- [ ] `auth.audit_log` partitioned by month
- [ ] Grafana dashboard for auth (from Auth Dashboard section of platform plan)
- [ ] **Component 3b — Token binding** — single-use `jti` enforcement for one-time credentials only (DPoP proof `jti`, approval tokens — Phase 13; bearer JWTs are never replay-windowed); `cnf` claim enforcement for mTLS-bound (internal services) + DPoP (external SDK clients) (Phase 13)
- [ ] **Component 5c — Behavioral Constraints Engine** — staging per Component 5c (authoritative): Phase 2 = `auth.behavior_policies` table + ONE shadow seed row; Phase 10 = alert-only middleware; Phase 13 = blocking/quarantine, per-tenant policies, sequence rules, anomaly signals
- [ ] **Component 8c — Workload Identity Attestation** — SPIRE deployment, `auth.agent_workload_acl`, SVID-bound service tokens, Sigstore-keyless for external developers (Phase 13)
- [ ] **Component 8d — A2A Delegation Chain Validation** — chain signature verification, depth cap, expiry inheritance, transitive flag, in-chain cycle detection (Phase 10)
- [ ] **Component 9b — Policy Compilation (OPA bundles)** — bundle builder, S3 distribution, OPA sidecar pattern, 30-day dual-write migration (Phase 13)
- [ ] **Component 10 — Step-Up Approval Authorization** — `auth.approval_requests` + `auth.approval_grants` schema (⚡ ship schema in first-cycle); full request → step-up → grant → consume flow (Phase 9C, when human-in-the-loop ships)
- [ ] New error codes added to Contract 2: `APPROVAL_REQUIRED`, `APPROVAL_EXHAUSTED`, `APPROVAL_INVALID`, `DELEGATION_CHAIN_INVALID`, `DELEGATION_CYCLE`, `TOKEN_REPLAYED`, `TOKEN_BINDING_MISMATCH`, `BEHAVIORAL_LIMIT`, `AGENT_QUARANTINED`, `TOKEN_REVOKED`, `KEY_REVOKED`, `PX0_TOKEN_INVALID`, `STEP_UP_REQUIRED`, `TENANT_UNKNOWN`
- [ ] **Emergency signing-key rotation drill** — quarterly on staging; forge bad-kid token after rotation, assert every verifier returns `KEY_REVOKED`
- [ ] **Tenant-wide token revocation** — `POST /v1/tenants/{tenant_id}/revoke-all-tokens` (depends on px0 bridge; Phase 11+)
- [ ] **Federated upstream identity** — `auth.upstream_identity` accepts additional issuers (Okta, Azure AD, custom OIDC) for enterprise SSO; Component 11 risk-based `acr` policies
- [ ] **px0 user-revocation propagation** — Kafka consumer for `px0.user.deactivated` → invalidates all approval grants by that user, audit-log row written

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

Verdicts from a per-concern review of this phase. The design above is unchanged; this section is the canonical place for accepted post-review mitigations. Items apply during Phase 13 hardening unless noted.

### 1. Auth Service Over-Centralization — REAL
Evidence: lines 1–51 (component overview). Auth-service owns login, JWT mint, JWKS, authorization, behavioral, audit, approval, federation, key rotation — concentrated blast radius.
**Mitigation (Phase 13):** extract behavioral constraints (Component 5c) and approval workflows (Component 10) into partner services consumed async via Kafka. Auth retains JWT mint + RBAC/ABAC + token lifecycle. Move audit aggregation to a worker reading the outbox.

### 2. Centralized Authorization Bottleneck — REAL
Evidence: lines 1807–1827; Component 9b (line 1551) defers OPA bundles to Phase 13.
**Mitigation:** dual-write to OPA bundle format end of first cycle to shorten cutover. Services pre-warm a per-process decision cache (60 s TTL) with negative-result caching and jittered backoff on misses.

### 3. Behavioral Engine Scaling Risk — REAL
Evidence: lines 822–934 (Component 5c) — counters/sequences/anomaly scoring inline per action.
**Mitigation:** at `behav-cnt:*` cardinality >100 k/min, switch anomaly scoring to async batch (Kafka micro-batches); apply in-process sliding-window aggregation for rate counters.

### 4. Excessive Valkey Dependency Concentration — REAL
Evidence: lines 60, 372, 527, 583, 729, 756, 768, 773, 897–900. Valkey backs JWT blacklist, agent-cap cache, jti replay, revocation, rate limits, decision cache, behavioral counters.
**Mitigation:** (a) nightly snapshot of revoked-token set to S3 for cold-start bootstrap; (b) emit `cypherx.auth.valkey.unhealthy` so upstream services switch to local fallback bundles; (c) document max acceptable Valkey outage (≤30 min) in runbook.

### 5. Audit Chain Write Contention Risk — REAL
Evidence: lines 975–1005. Per-tenant chain uses WATCH/MULTI on `audit-chain-tip:{tenant_id}` (lines 985–989) — high-volume tenants will hotspot.
**Mitigation (Phase 13):** at >100 events/s per tenant — (a) batch hash-chain updates (compute chain hash every 100 ms over N rows); (b) treat S3 Object Lock as durability anchor; in-DB chain is verification cache only.

### 6. Eventual Consistency Coordination Complexity — REAL
Evidence: lines 155–166, 282–284. Tenant/policy events broadcast over Kafka without bounded-staleness SLA.
**Mitigation:** publish per-service 99p staleness SLA (target ≤5 s) for quota/policy/suspension events. Cross-service integration test asserts policy change visible to all consumers within SLA.

### 7. Hardening Migration Complexity — REAL
Evidence: lines 1945–2017. Seven critical security components (3b token binding, 5c behavioral block, 8c SPIFFE, 8d chain validation, 9b OPA distribution, 10 approval enforcement, 11 federated IdPs) gated on Phase 13.
**Mitigation:** front-load SPIFFE (8c) before multi-region scale; ship 5c in Phase 10 in alert-only mode (30-day tuning); run Phase-13 approval-flow integration tests during Phase 9.
**2026-06 amendment alignment:** the 5c staging here is now normative in Component 5c itself (Phase 2 = table + one shadow seed row; Phase 10 = alert-only middleware; Phase 13 = blocking/quarantine). Component 11 is config-gated rather than Phase-13-blocked: the empty `auth.upstream_identity` table disables px0 verification cleanly, and the px0 seed lands in Phase 11.
