# Multi-tenancy, RLS & security

> One tenant per organization over a **shared** knowledge graph, isolated by Postgres Row Level Security keyed on `app.tenant_id` (Contract 13) and layered with an **app-owned** per-repo/team authorization table (`cypherx_a1.resource_acls`). `tenant_id` is taken **only** from the verified agent JWT — never a request body. The non-superuser runtime role `cxa1_user` runs every tenant-scoped query inside `in_tenant()` (`SELECT set_config('app.tenant_id', ..., true)`), under `ENABLE` + **`FORCE`** RLS with a `NULLIF(...)::uuid` guard so an unset GUC returns **no rows** (never errors, never leaks). Connector credentials are sealed at rest; the copilot is screened by fail-closed guardrails; ingestion lands an immutable audit trail; and every new tenant-scoped table MUST ship a cross-tenant-denial test in CI.

This document is the authoritative security reference for cypherx-a1 ("Autonomous Engineering Memory"). Every claim is grounded in real code: `src/cypherx_a1/db/pool.py`, `src/cypherx_a1/core/auth.py`, `src/cypherx_a1/db/ingest_repo.py`, `src/cypherx_a1/api/{connectors,webhooks}.py`, `src/cypherx_a1/services/guardrails_client.py`, and the migration `db/migrations/20260614_0001__init.sql`. Where the MVP defers a layer to a seam, that is called out explicitly.

---

## 1. Threat model & the four isolation layers

cypherx-a1 is a multi-tenant consuming app (peer of `xAgent/ax-1`), not a SharedCore service. Its primary security obligation is **tenant isolation over a shared database**, with a finer-grained authorization layer for sensitive engineering resources (private repos, team-scoped knowledge). The defenses are layered so that a failure in one does not by itself cause a cross-tenant leak.

| Layer | Mechanism | What it stops | Code |
|-------|-----------|---------------|------|
| 1. Identity | Inbound agent JWT re-verified locally against Auth JWKS (RS256); `tenant_id` read from the token claim only | Spoofed tenant via body/header; forged/expired/wrong-audience tokens | `core/auth.py` |
| 2. Tenant RLS | `ENABLE` + `FORCE` RLS, policy `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`, set per-transaction by `in_tenant()` | Cross-tenant row read/write — even from app bugs that forget a `WHERE tenant_id` | `db/pool.py`, `*__init.sql` |
| 3. Resource ACL | App-owned `cypherx_a1.resource_acls` (per-repo / per-team / per-service read rules) | Intra-tenant over-sharing of private repos/teams | `resource_acls` table (enforcement seam) |
| 4. Data-at-rest / egress | Sealed connector secrets, fail-closed guardrails on copilot, PII screening, immutable raw-event audit | Credential theft, prompt-injection/PII exfiltration, untraceable ingestion | `connector_secrets`, `guardrails_client.py`, `raw_events` |

The crown-jewel invariant from the platform design holds: **the graph is app-owned and never enters RAG or Memory.** That is not only a quality decision — it is a security boundary. RAG chunks are opaque text+metadata; Memory is per-principal episodic. Keeping the graph out of both prevents cross-principal leakage through Memory and embedding-cost/exfiltration vectors through RAG.

---

## 2. The tenant model — one org, shared graph, ACL overlay

The locked tenancy decision is: **one tenant per organization, a shared graph within the tenant, plus app-owned per-repo/team ACLs.**

- **One tenant = one organization.** A `tenant_id` (UUID) is the unit of isolation. All of an org's engineering history — people, services, repos, PRs, tickets, incidents, decisions — lives in one tenant's slice of the shared tables.
- **Shared graph within the tenant.** Every member/agent of the org queries the same `entities`/`edges` graph for that tenant. There is no per-user graph fork; that would defeat the "who built what across the org" product premise.
- **ACL overlay for sensitivity.** Because a shared graph would otherwise expose private-repo or team-internal knowledge to everyone in the org, `cypherx_a1.resource_acls` carries per-resource read rules (see §7). Auth (SharedCore) deliberately does **not** model repos or teams — that is application domain knowledge, owned here.

### Tenant identity comes only from the JWT

`core/auth.py` resolves the principal from the verified bearer token. The `tenant_id` is the JWT `tenant_id` claim; if it is absent the request is rejected `401`:

```python
tenant_id = claims.get("tenant_id")
if not tenant_id:
    raise ApiError(ErrorCode.UNAUTHORIZED, "Agent token missing tenant_id claim.")
```

There is **no** code path that reads `tenant_id` (or `agent_id`) from a request body or query string for authenticated routes. API request models are `extra="forbid"` (pydantic v2), so a client cannot even smuggle a `tenant_id` field into a body — it is a validation error. This is Contract 13's core rule: *identity from the token, never the payload.*

The single exception is the **webhook path** (`POST /webhooks/{kind}?tenant=<uuid>`), which carries no inbound agent JWT. There, the tenant binding is in the URL and is authenticated by the HMAC **signature** over the body, not a token — see §8. The webhook path is graph-only (no RAG embed) precisely because it has no agent JWT to forward downstream.

---

## 3. Identity verification (Layer 1)

`require_principal` is the FastAPI dependency on every authenticated route. It does two things: verify the JWT and check the revocation mirror.

### 3.1 JWT verification (`_decode`)

cypherx-a1 is edge-facing: callers (the frontend BFF / edge, or an external api-key-exchanged JWT) present a **bare agent JWT** in `Authorization: Bearer ...`. The service **re-verifies it locally** against the Auth JWKS — defense in depth, the same posture as xAgent/llms/guardrails/rag. Verification asserts:

| Check | Value / source |
|-------|----------------|
| Algorithm | `RS256` only |
| Signature | JWKS signing key resolved by the token `kid` (`PyJWKClient`, 5-min key cache, refresh-on-kid-miss) |
| `iss` | must equal `settings.auth_issuer_url` |
| `aud` | must contain `settings.auth_platform_audience` |
| Required claims | `exp`, `iss`, `aud`, `sub` (`options={"require": [...]}`) |
| Clock skew | `leeway = 60s` (`_CLOCK_SKEW_SECONDS`) |
| `tenant_id` | must be present (else `401`) |

Any `PyJWTError` or JWKS fetch failure raises `ApiError(ErrorCode.UNAUTHORIZED, ...)`. The verified bearer is preserved on `Principal.raw_token` and forwarded **verbatim** as `X-Forwarded-Agent-JWT` on every downstream SharedCore call (Contract 12) — the body carries no identity.

The JWKS client is process-cached per `jwks_url` (`get_jwks_client`) and warmed at startup (`warm_jwks`, best-effort). `/readyz` gates on a warm JWKS in addition to Postgres reachability.

### 3.2 Scope gating

The resolved `Principal` must hold at least one allowed scope (else `403`). Scopes are coarse at the dependency, fine at the endpoint:

| Scope | Constant | Grants |
|-------|----------|--------|
| `cypherxa1:query` | `SCOPE_QUERY` | read/query (copilot, graph reads) |
| `cypherxa1:ingest` | `SCOPE_INGEST` | connector sync + extraction |
| `cypherxa1:admin` | `SCOPE_ADMIN` | admin operations |

Platform scopes `agent:execute`, `agent:admin`, `platform:admin` are also admitted in `_BASE_ALLOWED_SCOPES` so an admin/platform JWT is not pre-emptively `403`'d at the dependency before an endpoint's own finer `require_scope(...)` check runs. Helpers compose the hierarchy: `query_scopes() ⊇ ingest_scopes() ⊇ admin_scopes()`. For example `POST /v1/connectors/{kind}/sync` and `POST /v1/extract` both call `require_scope(principal, ingest_scopes(), ...)`.

### 3.3 Revocation mirror (fail-open)

After signature/claims pass, the token runs through the shared Valkey revocation **mirror** (`_enforce_revocation`, Contract 1 / WP03). It looks up `jti` / `kid` / `agent_id` / `iat` against the `cypherx:rev:` key prefix. This control **fails open**: if Valkey is unavailable or slow (`revocation_valkey_timeout_seconds = 0.15`), the check is skipped (counted in `revocation_check_skipped_total`) and the request proceeds — availability wins for a soft mirror. A confirmed revocation raises `ApiError(ErrorCode.TOKEN_REVOKED)`. Revocation is **disabled** as a no-op when `revocation_check_enabled` is false.

> Note the deliberate asymmetry: **identity/JWT verification fails closed** (no valid token → no access), but the **revocation mirror fails open** (it is a best-effort low-latency optimization layered on top of `exp`-bounded tokens). Guardrails (§9) also fail closed.

### 3.4 Reserved JWT claims — accept but ignore

Per the platform Phase-13 hardening posture, cypherx-a1 **accepts but ignores** the reserved claims `cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, and `approval_context`. They are preserved in `Principal.raw_claims` (and thus forwarded), but **no logic gates on their presence or absence**. Do not start enforcing them here without a contract change.

---

## 4. Tenant RLS (Layer 2) — the heart of isolation

### 4.1 The runtime role

The app connects as `cxa1_user` (`DATABASE_URL`, Neon POOLED). This role is created idempotently in the init migration with **`LOGIN` only** — it is:

- **not** a superuser,
- granted **no** `BYPASSRLS`,
- granted **no** `CREATE EXTENSION` (the image is the frozen `pgvector/pgvector:pg16`; extensions are created by the migration role `cxa1_ddl` only).

Because it cannot bypass RLS, the policies below are inescapable from application code — there is no privileged escape hatch in the runtime path.

### 4.2 `in_tenant()` — `SET LOCAL` via `set_config(..., true)`

Every tenant-scoped query runs inside `in_tenant()` (`db/pool.py`), which opens one transaction and sets the tenant GUC transaction-locally before running the caller's work:

```python
async def in_tenant[T](pool, tenant_id, fn) -> T:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
        return await fn(conn)
```

The third argument `true` makes the setting **transaction-local** — exactly equivalent to `SET LOCAL`. This is mandatory for the Neon **POOLED** (transaction-mode) endpoint: a transaction-local GUC is scoped to and reset at the end of the transaction, so a pooled connection handed to the next checkout never carries a stale `app.tenant_id`. (Contract 13's reason the app DSN must use the pooled/transaction-mode endpoint, while the migrate job uses the DIRECT/session endpoint for advisory locks.)

The `tenant_id` passed to `in_tenant()` always comes from `principal.tenant_id` (the verified JWT claim). Callers never thread a body-supplied tenant. Example from `api/connectors.py`:

```python
connector_id = await in_tenant(pool, principal.tenant_id, _ensure)
```

`readyz_ping()` runs a bare `SELECT 1` **outside** any tenant scope purely as a liveness/reachability probe — it touches no tenant-scoped table, so RLS is irrelevant there.

### 4.3 `ENABLE` + `FORCE` RLS + the `NULLIF` guard

The init migration enables RLS on every tenant-scoped table and immediately **forces** it, then installs an identical isolation policy per table:

```sql
EXECUTE format('ALTER TABLE cypherx_a1.%I ENABLE ROW LEVEL SECURITY', t);
EXECUTE format('ALTER TABLE cypherx_a1.%I FORCE  ROW LEVEL SECURITY', t);
EXECUTE format(
  'CREATE POLICY %I_isolation ON cypherx_a1.%I FOR ALL '
  'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
  'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
  t, t);
```

Three details carry the security weight:

- **`FORCE ROW LEVEL SECURITY`** — without `FORCE`, RLS is bypassed for the **table owner**. By forcing it, the policy applies even if `cxa1_user` were ever (accidentally) the table owner. Defense in depth: there is no owner escape hatch.
- **`current_setting('app.tenant_id', true)`** — the second arg `true` = *missing_ok*. If the GUC was never set, `current_setting` returns `NULL` instead of raising `unrecognized configuration parameter`. This means a query that forgot `in_tenant()` does not error out with a confusing exception — it simply matches nothing.
- **`NULLIF(..., '')::uuid`** — guards the empty-string case (a GUC set to `''`). `NULLIF('', '')` → `NULL`, and `tenant_id = NULL` is never true, so the policy admits **zero rows**. The net effect: **no tenant context ⇒ no rows**, on both `USING` (reads/updates/deletes) and `WITH CHECK` (inserts/updates). An insert with no tenant context, or with a mismatched `tenant_id`, is rejected.

`FOR ALL` means the same predicate governs `SELECT`, `INSERT`, `UPDATE`, and `DELETE`. The `WITH CHECK` half is what stops a tenant from **writing** a row stamped with another tenant's id, even if it could compute one.

### 4.4 The tenant-scoped table inventory

RLS (`ENABLE` + `FORCE` + `*_isolation` policy) is applied to exactly these eleven tables, each carrying a `tenant_id UUID NOT NULL` and a tenant-leading index:

| Table | Tenant-leading index / PK | Role grants (RLS on top) |
|-------|---------------------------|--------------------------|
| `entities` | `idx_entities_tenant`, `uq_entities_natural_current (tenant_id, kind, natural_key) WHERE valid_to IS NULL` | SELECT/INSERT/UPDATE/DELETE |
| `edges` | `idx_edges_src/dst (tenant_id, …, rel)`, `idx_edges_current … WHERE valid_to IS NULL` | SELECT/INSERT/UPDATE/DELETE |
| `identities` | `idx_identities_tenant`, `uq_identities (tenant_id, source, handle)` | SELECT/INSERT/UPDATE/DELETE |
| `raw_events` | `idx_raw_events_tenant`, `uq_raw_events (tenant_id, source, external_id, content_sha)` | **SELECT/INSERT only** (append-only audit) |
| `connectors` | `idx_connectors_tenant`, `uq_connectors (tenant_id, kind, display_name)` | SELECT/INSERT/UPDATE/DELETE |
| `connector_secrets` | `idx_connector_secrets_tenant` (PK `connector_id`, 1:1) | SELECT/INSERT/UPDATE/DELETE |
| `sync_cursors` | PK `(tenant_id, connector_id, stream)` | SELECT/INSERT/UPDATE/DELETE |
| `extraction_jobs` | PK `(tenant_id, node_id, content_sha, extractor_version)` | **SELECT/INSERT/UPDATE** (no DELETE — cost ledger) |
| `citations` | `idx_citations_tenant`, `idx_citations_chunk` | **SELECT/INSERT/DELETE** (no UPDATE) |
| `resource_acls` | `idx_resource_acls_lookup`, `uq_resource_acls (tenant, resource_type, resource_key, principal_type, principal_id)` | SELECT/INSERT/UPDATE/DELETE |
| `rag_kbs` | PK `(tenant_id, logical_name)` | SELECT/INSERT/UPDATE/DELETE |

Note the GRANT minimization aligns with intent: `raw_events` is append-only audit (no UPDATE/DELETE), `extraction_jobs` is a cost ledger (no DELETE), `citations` are replace-by-delete (no UPDATE). RLS sits **on top of** these grants — both must permit an action.

### 4.5 The deliberate non-RLS table: `outbox`

`cypherx_a1.outbox` (Contract 5 publish queue) **has RLS explicitly disabled**:

```sql
ALTER TABLE cypherx_a1.outbox DISABLE ROW LEVEL SECURITY;
```

This is intentional, not an oversight. The outbox is drained by a background publisher that runs **across all tenants** and sets **no** `app.tenant_id`; tenant-RLS would block the drain entirely. Isolation is in the **payload**, not the row: each envelope carries `partition_key = tenant_id` (Contract 5) and a tenant-scoped body. The table is in the same schema and only `cxa1_user` (SELECT/INSERT/UPDATE) can touch it. Every other write to the outbox happens inside the producing transaction (transactional outbox), so the cross-tenant queue is a controlled internal seam, never a query surface exposed to a caller.

### 4.6 How the `NULLIF` guard appears in writes

Inserts re-derive the tenant from the GUC rather than trusting a parameter, so the value written always matches the RLS context. Every `ingest_repo` insert uses the same idiom, e.g. `record_raw_event`:

```sql
INSERT INTO cypherx_a1.raw_events
    (tenant_id, source, external_id, record_type, content_sha, payload)
VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s, %s)
ON CONFLICT (tenant_id, source, external_id, content_sha) DO NOTHING
```

The `tenant_id` column value is computed from the session GUC, identical to the RLS predicate. There is no way for the application to insert a row for tenant B while scoped to tenant A: the computed `tenant_id` is A, and the `WITH CHECK` would reject any other value anyway. The same pattern appears in `get_or_create_connector`, `set_cursor`, `record_extraction_job`, `set_rag_kb`, and `add_citation`.

---

## 5. Bitemporal isolation & the "current slice"

The graph is **bitemporal**: `entities` and `edges` keep historical versions with `valid_from`/`valid_to`, where `valid_to IS NULL` marks the current row. This interacts with tenancy in one subtle way worth stating: RLS scopes by `tenant_id`, and the **current-slice** uniqueness is a *partial* unique index, also tenant-leading:

```sql
CREATE UNIQUE INDEX uq_entities_natural_current
  ON cypherx_a1.entities (tenant_id, kind, natural_key) WHERE valid_to IS NULL;
```

So "one current entity per `(tenant, kind, natural_key)`" is enforced **within** a tenant — tenant A and tenant B can each have a current `repo` with `natural_key = "octo/api"` without collision, and neither can see the other's. Reads in `ingest_repo`/`graph_repo` (e.g. `entities_for_docs`, `list_unextracted_entities`) consistently filter `e.valid_to IS NULL` for the current slice, and RLS filters the tenant — the two predicates compose.

---

## 6. Identity resolution & the per-tenant alias namespace

`cypherx_a1.identities` resolves cross-tool aliases (a GitHub login, a Slack uid, a Jira account, an email) to a canonical `person` entity. Its uniqueness is `uq_identities (tenant_id, source, handle)` — the alias namespace is **per-tenant**. The same GitHub login in two orgs maps to two different `person` entities, and RLS prevents either tenant from resolving into the other's identity table. Identity merging is therefore always intra-tenant; there is no global person registry that could leak who-is-who across orgs.

---

## 7. The app-owned `resource_acls` authorization layer (Layer 3)

A shared graph within a tenant is the right product default, but some engineering knowledge is sensitive **within** the org (private repos, security-team incidents). `cypherx_a1.resource_acls` is the app-owned authorization overlay that the platform Auth service deliberately does not provide — **Auth never models repos or teams.**

### 7.1 Schema

```sql
CREATE TABLE cypherx_a1.resource_acls (
  acl_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID        NOT NULL,
  resource_type  VARCHAR(20) NOT NULL,   -- repo | team | service
  resource_key   TEXT        NOT NULL,   -- e.g. "owner/name"
  principal_type VARCHAR(20) NOT NULL,   -- agent | user | role | tenant
  principal_id   TEXT        NOT NULL,   -- id or '*'
  permission     VARCHAR(20) NOT NULL DEFAULT 'read',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT resource_acls_principal_enum CHECK (principal_type IN ('agent','user','role','tenant')),
  CONSTRAINT uq_resource_acls UNIQUE (tenant_id, resource_type, resource_key, principal_type, principal_id)
);
CREATE INDEX idx_resource_acls_lookup
  ON cypherx_a1.resource_acls (tenant_id, resource_type, resource_key);
```

Semantics:

| Column | Meaning |
|--------|---------|
| `resource_type` | the kind of engineering resource: `repo`, `team`, or `service` |
| `resource_key` | the resource's natural key (e.g. `"owner/name"` for a repo) |
| `principal_type` | who the grant is to: `agent`, `user`, `role`, or `tenant` |
| `principal_id` | the specific id, or `'*'` for "everyone in the tenant" |
| `permission` | currently `read` (the only meaningful grant for a read-only memory) |

The unique constraint makes each `(tenant, resource_type, resource_key, principal_type, principal_id)` grant idempotent. The lookup index `(tenant_id, resource_type, resource_key)` is built to answer "who may read repo X in this tenant" cheaply.

### 7.2 Layering, not replacement

`resource_acls` is **RLS-scoped like every other tenant table** — it lives under the same `_isolation` policy, so ACLs are themselves tenant-isolated. The ACL check is a *second* gate applied **after** RLS has already confined the query to the tenant: RLS answers "is this row in my tenant?", the ACL answers "within my tenant, am I allowed to read this repo/team's knowledge?". A missing or `'*'` grant defaults to the shared-graph behavior; a restrictive grant narrows it.

### 7.3 MVP status (enforcement seam)

The `resource_acls` **table, RLS, grants, and index ship in the MVP migration**. The query-time enforcement helper (filtering retrieval results by the caller's grants) is a documented **application seam** for the access-control hardening phase — there is no `resource_acls` read helper in `src/cypherx_a1/db/` yet, by design, because the MVP slice operates on the shared-graph default. When the enforcement layer lands it MUST:

1. resolve the caller's `principal_type`/`principal_id`/roles from the verified `Principal`,
2. run inside `in_tenant()` like every other access (so the ACL lookup is itself tenant-scoped),
3. filter retrieval/graph results to resources the principal may `read` (an entity's `repo`/`service`/`team` provenance ∈ the grant set, or a `'*'` grant exists),
4. fail closed for resources marked sensitive with no matching grant.

Until then, do **not** assume the absence of an ACL row means "deny" in product behavior — the MVP default is shared-graph "allow within tenant", and RLS remains the hard isolation boundary.

---

## 8. Connector-secret sealing & rotation (Layer 4 — credentials)

Connector credentials (e.g. a GitHub token, a webhook signing secret) are the highest-value secrets in the app: they grant read access to a customer's source-of-truth. They are stored sealed, separated from non-secret config.

### 8.1 Table split: config vs. secret

```sql
-- non-secret config (org, repos, urls) — readable for operations
cypherx_a1.connectors        (connector_id, tenant_id, kind, display_name, config JSONB, status, ...)
-- the sealed credential, 1:1 with a connector, isolated
cypherx_a1.connector_secrets (connector_id PK, tenant_id, sealed_value TEXT, created_at, rotated_at)
```

`connectors.config` holds **only** non-secret connector configuration (org name, repo list, base URLs). The credential lives in a **separate** table, `connector_secrets`, 1:1 with the connector (`connector_id` is the PK). Both are RLS-isolated by tenant. This split means an operational read of connector config never touches the secret, and the secret table can carry its own audit columns (`created_at`, `rotated_at`).

### 8.2 The `sealed:v1` envelope

`connector_secrets.sealed_value` is an opaque sealed string. The init migration documents the two recognized forms:

| Form | Meaning |
|------|---------|
| `sealed:v1:<...>` | a KMS/BYOK-sealed envelope — the credential encrypted at rest under a tenant/platform key (the `v1` is a versioned envelope so the sealing scheme can rotate without a schema change) |
| `env:<NAME>` | an indirection to an environment variable (`<NAME>`) — used in local/dev where the credential is injected by Doppler, so no ciphertext is persisted in the DB at all |

A plaintext credential is **never** written into `sealed_value`. The `v1` prefix is deliberate forward-compat: a future `sealed:v2` (different KEK, different AEAD) can coexist row-by-row, and the unsealer dispatches on the prefix. The runtime role can read its own tenant's sealed value (RLS-scoped), unseal it in-process to call the source, and never logs the plaintext.

### 8.3 Rotation

Rotation is a credential-update operation that **re-seals** and stamps `rotated_at`:

- the new credential is sealed into a fresh `sealed:v1:<...>` (or repointed `env:<NAME>`) envelope and written to `sealed_value`,
- `rotated_at` is set to `NOW()` (it is `NULL` until the first rotation),
- the operation runs inside `in_tenant()` so it can only touch the calling tenant's secret, and only a principal with the admin/ingest scope may perform it.

Because the envelope is versioned, rotation can also mean *rolling the sealing key*: re-seal existing `v1` rows under a new KEK and bump to `v2`, with `rotated_at` recording the migration time. The `created_at`/`rotated_at` pair is the at-rest audit trail for the credential's lifecycle.

> MVP note: in the keyless default (`CONNECTOR_MODE=mock`), connectors run on bundled GitHub fixtures and no real credential is sealed. With `CONNECTOR_MODE=live`, `GITHUB_TOKEN` is supplied via env (the `env:<NAME>` form), and the `sealed:v1` KMS path is the cloud hardening target.

### 8.4 Webhook signature secret (`GITHUB_WEBHOOK_SECRET`)

The webhook receiver authenticates inbound deliveries by HMAC, not a token. `GithubConnector.verify_signature` computes `sha256=HMAC-SHA256(secret, raw_body)` and compares with a **constant-time** `hmac.compare_digest` against `X-Hub-Signature-256`:

```python
expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
return hmac.compare_digest(expected, sig)
```

A missing secret or a malformed/mismatched signature returns `False` → the receiver raises `401 "Invalid webhook signature."`. The tenant is bound by the URL (`?tenant=<uuid>`); the signature authenticates the payload (MVP). Production hardens this to a per-connector-install **path token** so the tenant binding is itself unguessable and per-install revocable.

---

## 9. Copilot screening & PII handling (Layer 4 — egress)

The copilot is the path where org-internal engineering knowledge is rendered into natural language and returned to a caller — the highest-risk egress surface for prompt-injection and PII leakage. It is wrapped in **fail-closed** guardrails on both sides.

### 9.1 Pre/post guardrail screening

`GuardrailsClient` calls the SharedCore guardrails service:

| Call | Endpoint | Body |
|------|----------|------|
| Pre (input) | `POST /v1/check/input` | `{ "text", "task_id" }` |
| Post (output) | `POST /v1/check/output` | `{ "text", "input_text", "task_id" }` |

Identity flows in **headers only** (Contract 12/13): the service JWT in `Authorization`, the forwarded agent JWT in `X-Forwarded-Agent-JWT`, plus W3C trace headers. The body carries **no** identity. The post-check passes the original `input_text` alongside the generated `text` so output filtering can reason about the request that produced the answer.

### 9.2 Fail-closed semantics

A guardrail is a safety control, so the client treats anything ambiguous as a hard failure — it never silently "allows":

- a transport/`HTTPError` → `503 SERVICE_UNAVAILABLE`,
- any `>= 400` response → `503` (never interpreted as allow),
- a `2xx` body whose `decision` is not one of `allow | warn | redact | block` → `503` ("failing closed").

A valid `decision = "block"` is mapped by the copilot caller to **`422 GUARDRAIL_VIOLATION`** — the answer is refused. `redact`/`warn` decisions carry `processed_text` (the sanitized text the copilot uses) and `violations`. This is the platform-standard guardrail posture, a direct port of the xAgent ax-1 client.

### 9.3 PII handling

PII screening is delegated to guardrails (the platform's safety-of-record), applied on both the inbound query and the outbound answer:

- **Inbound:** the user's question is screened before it is used to build the LLM prompt, catching attempts to inject or surface secrets/PII.
- **Outbound:** the generated answer (with its citations) is screened before return; a `redact` decision returns `processed_text` with PII masked, a `block` decision refuses the answer (`422`).

Because the copilot only ever answers from the tenant's own RLS-scoped graph + RAG corpus, and because the graph never enters Memory/RAG, PII exposure is bounded to *the tenant's own data, screened on the way out*. Memory (conversational working memory) is **best-effort and never fails an answer**, so a Memory outage cannot become a PII bypass — the guardrail still runs.

---

## 10. Audit trail & eventing

cypherx-a1 keeps an immutable ingestion audit and a tenant-partitioned event stream.

### 10.1 `raw_events` — immutable landing / audit

Every ingested artifact lands first in `cypherx_a1.raw_events`, **append-only** (the runtime role has only `SELECT, INSERT` — no UPDATE/DELETE). Landing is idempotent on `(tenant_id, source, external_id, content_sha)` via `ON CONFLICT … DO NOTHING`, and `record_raw_event` returns whether the row was newly inserted so the pipeline skips re-processing duplicates. This table is the durable "what did we ingest, when, and from where" audit (`received_at`, `source`, `external_id`, `content_sha`), per tenant, that cannot be silently rewritten.

### 10.2 `extraction_jobs` — cost ledger / audit

The LLM extraction pass writes an idempotent ledger row keyed `(tenant_id, node_id, content_sha, extractor_version)`. It records `llm_call_id` (the **gateway billing key**, Contract 19 — never rewritten by this app), `cost_usd`, `edges_extracted`, and `status`. The runtime role has `SELECT, INSERT, UPDATE` but **no DELETE** — the cost/audit ledger is not erasable. This both deduplicates extraction (a completed job at the current `extractor_version` is skipped) and provides a per-tenant cost audit.

### 10.3 Outbox events (Contract 5)

State changes emit Contract-5 envelopes through the transactional `outbox` to `cypherx.cypherxa1.*` topics (`record.normalized`, `usage.recorded`, each with a paired `.dlq`). `partition_key = tenant_id`. As covered in §4.5, the outbox is the one non-RLS table; isolation is in the envelope payload + partition key. Per-invocation MCP tool metering is the **caller's** (xAgent's) outbox, never the stateless `mcp-eng-memory` tool — the product meters only its own usage.

### 10.4 Structured logs

Logs are Contract-6 structured JSON (structlog). Auth events emit non-PII fields: `jwks_warmed`, `token_revoked` (`agent_id`, `tenant_id`), `revocation_check_skipped`, `guardrails_call_rejected`. Raw credentials and unsealed secrets are never logged. `trace_id` (Contract 8 W3C `traceparent`) is propagated through every downstream call and Kafka event for cross-service correlation.

---

## 11. CI requirement: cross-tenant-denial test per new table

This is a **hard gate**, not a guideline.

> **Every new tenant-scoped table MUST ship with a cross-tenant-denial test before it can be added to CI-green.**

The test asserts the negative space that RLS guarantees. The canonical shape:

1. Within `in_tenant(pool, TENANT_A, ...)`, insert a row.
2. Within `in_tenant(pool, TENANT_B, ...)`, attempt to `SELECT` that row → **0 rows** (RLS `USING`).
3. Within `in_tenant(pool, TENANT_B, ...)`, attempt to `INSERT`/`UPDATE` a row stamped with `TENANT_A` → **rejected** (RLS `WITH CHECK`).
4. With **no** `app.tenant_id` set, `SELECT` the row → **0 rows** (the `NULLIF` guard: unset GUC ⇒ no rows, no error).

A new table is not "done" until:

- it has `tenant_id UUID NOT NULL` and a tenant-leading index/PK,
- it is added to the `FOREACH t IN ARRAY ARRAY[...]` list in the init migration (so it gets `ENABLE` + `FORCE` RLS + a `_isolation` policy),
- writes derive `tenant_id` from `NULLIF(current_setting('app.tenant_id', true), '')::uuid` (never a trusted parameter),
- grants are minimized to the operations the table actually needs (mirror the `raw_events`/`extraction_jobs`/`citations` precedent),
- **and** the four-step cross-tenant-denial test above is added and passing.

The only sanctioned exception is a genuinely cross-tenant internal queue like `outbox`, which must instead (a) explicitly `DISABLE ROW LEVEL SECURITY` with an inline comment justifying it, (b) carry isolation in the payload + `partition_key`, and (c) be touched only by the internal publisher path — never a caller-facing query.

---

## 12. What this app does NOT do (boundary reminders)

These are security-relevant boundaries inherited from the platform decisions; do not "fix" them:

- **No tenant from the body.** `tenant_id`/`agent_id` come only from the verified JWT. Request models are `extra="forbid"`.
- **No graph in RAG or Memory.** The graph is app-owned and stays in `cypherx_a1`. RAG holds only opaque chunks; Memory is per-principal conversational scratch. This prevents cross-principal leakage and embedding-cost/exfiltration channels. A code-review/lint guard keeps the corpus out of Memory.
- **No business logic in SharedCore.** Auth does not model repos/teams — `resource_acls` is the app's job. The app never pushes its authorization model upstream.
- **No `BYPASSRLS`, no `CREATE EXTENSION` at runtime.** `cxa1_user` is `LOGIN`-only; extensions and DDL belong to the migration role `cxa1_ddl`. The graph stays adjacency-list + recursive-CTE on the frozen `pgvector/pgvector:pg16` image (no Apache AGE/ltree).
- **No metering rewrite.** `llm_call_id` is the gateway's billing key; the app records it but never recomputes the gateway's cost (Contract 19).
- **Reserved JWT claims are accepted but never gated on** (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`).

---

## 13. Summary

Tenant isolation in cypherx-a1 is **structural, not advisory**: the runtime role cannot bypass RLS, `FORCE` RLS removes the owner escape hatch, the `NULLIF(...)::uuid` guard turns "no tenant context" into "no rows" instead of an error or a leak, and the tenant id is sourced only from a locally re-verified JWT. On top of that hard boundary sit an app-owned `resource_acls` overlay for intra-tenant repo/team sensitivity, sealed-and-rotatable connector credentials, fail-closed guardrail screening (with PII handling) on the copilot, an immutable raw-event + cost-ledger audit trail, and a CI gate that demands a cross-tenant-denial proof for every new tenant-scoped table. The `outbox` is the single, deliberate, documented non-RLS exception — a cross-tenant internal queue whose isolation lives in the Contract-5 payload and `partition_key`.
