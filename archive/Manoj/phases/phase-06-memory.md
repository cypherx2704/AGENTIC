# Phase 6 — SharedCore / Memory
> **Status:** ⏳ Pending | **Depends On:** Phase 0, 1, 2, 3 **+ LLMs pre-work package WP06** (declared dependency — see Amendment Log) | **Blocks:** Phase 9 (enhanced)
> **First Cycle:** 📋 Not required for very first cycle check. Required for Phase 9 enhanced pass.

## Amendment Log (2026-06 — pre-build reconciliation)

- **LLMs pre-work package (WP06) declared as an explicit dependency.** Phase 6 hard-depends on LLMs surfaces deferred in the Phase 3 first-cycle build: `POST /v1/embeddings` (256-item / 25 MiB caps, mock provider, usage + outbox), `GET /v1/models` with `embedding_dim`, the `embed` alias + embedding pricing seeds, and Valkey-backed Idempotency-Key replay. The Memory build may NOT start ahead of WP06.
- **xAgent MEMORY_WRITE integration corrected:** the Kafka topic `cypherx.memory.write.requested` (referenced by Phase 9, never defined or consumed here) is DELETED. xAgent's MEMORY_WRITE stage calls `POST /v1/memories` directly (async fire-and-forget HTTP with service JWT + `X-Forwarded-Agent-JWT`), reusing this service's validation + idempotency.
- **Components 2 / 2b user-scope check rewritten** to the post-Round-2 model: the user-scope read path consults `memory.tenant_config.user_scope_visibility`, default **`principal_only`** (JWT-resolved principal must match the writing principal); `tenant_shared` is the legacy opt-in. The old "tenant-shared by design" wording would have re-introduced the exact cross-end-user leak Round 2 fixed.
- **`memory.sessions` rebased onto the principal abstraction:** `agent_id` column replaced by `principal_type` + `principal_id` (standard resolution order `agent_id → api_key_id → app_id`); session ownership checks and `POST /v1/memories/sessions` updated accordingly. The session producer is the optional Contract-3 `input.session_id` field (see Phase 9 amendment).
- **GDPR wipe auth fixed for external callers:** `requested_by` is derived from the JWT chain in EITHER auth mode (external agent JWT via the gateway, or internal `X-Forwarded-Agent-JWT` / service-JWT `on_behalf_of`); the `X-Forwarded-Agent-JWT` header is no longer a hard requirement external callers cannot meet. Column renamed `requested_by_agent_id → requested_by_principal_id`.
- **Duplicate "Component 7b" renumbered:** Usage Metering is now **Component 7e** (Transactional Outbox keeps 7b); cross-references updated, including the stale "(Component 8 below)" pointer to the outbox.
- **Checklist hygiene:** the 📋 item "Per-agent ACL on user-scope memories (alternative to current tenant-shared default)" still described the pre-Round-2 default; reworded to reference Component 7d's `user_scope_acl` as an override of the (now `principal_only`-defaulted) visibility flag.
- **Quota enforcement single-owner dedupe:** quota ENFORCEMENT is ⚡ first-cycle in THIS phase (`memories_max`, `storage_bytes_max`, `stores_per_min`, `retrieves_per_min` — see the ⚡ checklist). The duplicate 📋 "Memory quota per tenant/user/agent" item is DELETED (tombstoned); Phase 13 Domain 3 only TUNES limit values and owns nothing canonical. Metering events carry **units + `request_id` ONLY** — billing joins/de-duplication on `request_id` happen downstream in the usage pipeline (the rule this phase already stated in prose, now normative for Phases 5 and 6).
- **Compose-parity runtime subsection added** (first-cycle runtime = docker compose + Neon + Valkey + Redpanda + MinIO): env vars via compose `.env` (Doppler = cloud form); an idempotent `topics-init` compose job (`rpk topic create`) stands in for Terraform-provisioned topics; Kong-fronted external auth and K8s/ArgoCD deploy are the **cloud (deploy-target) form** — first cycle verifies external agent JWTs directly against Auth JWKS and runs as a compose service with healthchecks. The affected ⚡ checklist items are restated accordingly.

---

## Phase Overview

SharedCore/Memory is the **persistent memory layer** that makes agents stateful across sessions. Without memory, every agent conversation starts fresh. Memory gives agents the ability to remember what happened, what users prefer, and what procedures worked — across days, weeks, and sessions.

**Deliverable:** A memory service supporting episodic and semantic memory types, with store/retrieve/delete operations and vector similarity search.

> **Declared dependency — LLMs pre-work package (WP06).** This phase consumes LLMs surfaces that are NOT part of the Phase 3 first-cycle build until WP06 lands: `POST /v1/embeddings` (256-item / 25 MiB caps, mock provider, usage + outbox), `GET /v1/models` with `embedding_dim`, the `embed` alias + embedding pricing seeds, and Valkey-backed Idempotency-Key replay. **The Memory build may not start ahead of WP06.**

> 🏗️ **Service Architecture Note:** The internal architecture of the memory service (memory extraction pipeline, auto-summarisation job, deduplication logic, memory consolidation scheduler) must be planned separately before implementation begins.

---

## High Level Design

### System Context

```
                        ┌──────────────────────────────────────────┐
                        │           MEMORY SERVICE                  │
                        │                                           │
  xAgent ──────────────►│  POST   /v1/memories            (store)        │
  xAgent ──────────────►│  POST   /v1/memories/retrieve   (semantic)     │
  xAgent ──────────────►│  POST   /v1/memories/sessions   (register sess)│
  xAgent ──────────────►│  POST   /v1/memories/extract    (auto, 📋)     │
                        │  GET    /v1/memories/{id}                       │
                        │  PUT    /v1/memories/{id}                       │
                        │  DELETE /v1/memories/{id}                       │
                        │  DELETE /v1/memories?scope=&scope_id=  (GDPR)   │
                        │  POST   /v1/memories/summarise  (📋)            │
                        └──────────────┬────────────────────────────┘
                                       │
               ┌───────────────────────┼──────────────────────┐
               ▼                       ▼                       ▼
       LLMs Gateway            pgvector (vectors)         PostgreSQL
    (extraction + embed       (memory embeddings)        (memory records
     + summarisation)                                     + metadata)
```

> **xAgent integration (Phase 9 MEMORY_WRITE) — corrected 2026-06:** xAgent's MEMORY_WRITE stage calls `POST /v1/memories` DIRECTLY — async fire-and-forget HTTP with service JWT + `X-Forwarded-Agent-JWT` — reusing this service's server-side validation and Idempotency-Key replay. There is **no Kafka memory-write-request topic**: the previously referenced `cypherx.memory.write.requested` is deleted (this phase never defined or consumed it; writes published to it would have vanished). Memory writes therefore have exactly ONE ingress: this HTTP API.

### Memory Types

| Type | What it stores | Scope | TTL |
|------|---------------|-------|-----|
| `episodic` | Records of past conversations/events | user or agent | 90 days default |
| `semantic` | Extracted facts ("user prefers Python") | user or agent | no expiry |
| `procedural` | Learned workflows ("steps to solve X") | agent | no expiry |
| `working` | Short-term session context extension | session | session end |

### Memory Scopes & `scope_id` ownership rules (CRITICAL — security boundary)

> **`principal` rename of `agent` (NEW for external operability).** The original `scope=agent` model baked the CypherX agent abstraction into Memory's ownership semantics. An external chat-app vendor whose product is "the app, not an agent" has no agent_id to bind to. To support both, Memory now uses **`principal`** as a generic identity type that can be an `agent_id`, an `api_key_id`, or an external `app_id`. The legacy `agent` value is accepted as a synonym for `principal_type=agent` and is auto-rewritten at write time. New code MUST use `principal`.

| Scope | `scope_id` meaning | Server-enforced ownership rule |
|-------|--------------------|-------------------------------|
| `tenant` | `JWT.tenant_id` (renamed from `global`) | `scope_id` MUST equal `JWT.tenant_id`. Visible to all principals of the tenant. |
| `principal` (was: `agent`) | The principal's UUID — derived from JWT in this order: `agent_id` → `api_key_id` → custom `app_id` claim | `scope_id` MUST equal the JWT's resolved principal_id (or caller has `platform:admin`). One principal CANNOT read another principal's memory. Reading agent-style memories from an api_key principal (or vice versa) is rejected. |
| `user` | Opaque tenant-local end-user identifier (CypherX does not own users) | `scope_id` is opaque. **Default behaviour:** per-principal isolation — only the principal who wrote the user-scope memory can read it (NOT tenant-shared). Tenant-shared user-scope (legacy default) is now opt-in via per-KB / per-tenant config flag `memory.tenant_config.user_scope_visibility = tenant_shared | principal_only` (default `principal_only` for new tenants; legacy migration sets `tenant_shared`). |
| `session` | A session UUID | The session must have been minted with this `principal_id` (tracked in `memory.sessions`). Cross-principal session reads rejected. |

> **Why `user_scope_visibility` defaults flipped.** External chat-app vendors building products on Memory expect "user A's memories are private to user A's session/principal" — a SaaS-default assumption. The original tenant-shared default would silently leak across end-users of an external customer's product. The new default is `principal_only`; tenants wanting cross-agent sharing (the legacy CypherX agent ecosystem use case) opt in.

> **Renamed `global` → `tenant`.** Original `global` had `scope_id = platform tenant UUID` — that wording confused "shared across the tenant" with "shared across the platform" and would have caused cross-tenant leak the first time someone read it literally. The new name forces clarity.

> **`scope_id` is always a UUID column at the DB layer** (`UUID NOT NULL`), even for the
> `user` scope where the HLD wording says "opaque tenant-local identifier". Opaque here
> means the platform doesn't enforce semantics — it does NOT mean variable-typed. For
> tenants whose native user identifier is not a UUID (SSO usernames, integer IDs from a
> legacy system, email addresses), the SDK and caller MUST map their identifier to a
> deterministic UUID BEFORE passing. Recommended construction:
> `uuid5(namespace = tenant_id, name = <native_user_id>)`.
> Same tenant + same native ID → same UUID, reproducibly, without a lookup table.
> This is a CALLER responsibility — the server performs no native-ID-to-UUID translation
> and cannot validate "is this UUID really a user?" for the `user` scope. Inconsistent
> mapping by the caller = the same human user appears under multiple scope_ids and
> their memory fragments silently.

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first. 📋 items design now, implement after.

---

### Component 1 — Memory Data Model ⚡

**PostgreSQL (`memory.tenant_config`) — pin embedding model per tenant (CRITICAL):**

```sql
CREATE TABLE memory.tenant_config (
  tenant_id                         UUID         PRIMARY KEY,
  default_embedding_model_alias     VARCHAR(100) NOT NULL DEFAULT 'embed',
  default_embedding_model_resolved  VARCHAR(100) NOT NULL,
  default_embedding_dim             INTEGER      NOT NULL,
  created_at                        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at                        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Row created lazily on the tenant's first memory store. Alias is resolved against
-- LLMs gateway /v1/models at creation; resolved fields are IMMUTABLE post-creation
-- until a future re-embed job (📋) is run by a platform admin. Without this pin,
-- two memories for the same tenant can land in different per-dim vector tables and
-- retrieve will miss half of them.
```

**PostgreSQL (`memory.memories`) — same dimension-shard pattern as RAG (Phase 5):**

```sql
-- Common metadata table
CREATE TABLE memory.memories (
  memory_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID         NOT NULL,
  scope            VARCHAR(20)  NOT NULL,
                   -- tenant | agent | user | session   (was: global | agent | user | session)
  scope_id         UUID         NOT NULL,
                   -- See "Memory Scopes & scope_id ownership rules" in HLD.
  memory_type      VARCHAR(20)  NOT NULL DEFAULT 'episodic',
                   -- episodic | semantic | procedural | working
  content          TEXT         NOT NULL,
  summary          TEXT,
  source           VARCHAR(50),
  importance       FLOAT        NOT NULL DEFAULT 0.5,
  embedding_model  VARCHAR(100) NOT NULL,   -- audit copy of tenant_config at write time
  embedding_dim    INTEGER      NOT NULL,
  tags             TEXT[]       NOT NULL DEFAULT '{}',
  metadata         JSONB        NOT NULL DEFAULT '{}',
  ttl_at           TIMESTAMPTZ,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  last_accessed_at TIMESTAMPTZ,

  -- NEW: principal_type identifies who "owns" the memory under principal-scope
  -- and is required at write time so reads can enforce per-principal isolation
  -- without re-resolving the writer's identity from auxiliary data.
  principal_type   VARCHAR(20),  -- NULL for tenant/session-scope; 'agent' | 'api_key' | 'app' for principal/user scope
  principal_id     UUID,         -- the principal who wrote the memory (NULL for tenant scope)

  CONSTRAINT importance_range   CHECK (importance >= 0.0 AND importance <= 1.0),
  CONSTRAINT scope_enum         CHECK (scope IN ('tenant','principal','agent','user','session')),
  CONSTRAINT memory_type_enum   CHECK (memory_type IN ('episodic','semantic','procedural','working')),
  CONSTRAINT principal_present  CHECK (
    (scope IN ('principal','agent','user') AND principal_type IS NOT NULL AND principal_id IS NOT NULL)
    OR scope IN ('tenant','session')
  )
);

CREATE INDEX idx_memories_tenant_scope ON memory.memories(tenant_id, scope, scope_id);
CREATE INDEX idx_memories_type         ON memory.memories(memory_type);
CREATE INDEX idx_memories_ttl          ON memory.memories(ttl_at) WHERE ttl_at IS NOT NULL;
CREATE INDEX idx_memories_metadata_gin ON memory.memories USING gin (metadata jsonb_path_ops);
CREATE INDEX idx_memories_tags_gin     ON memory.memories USING gin (tags);

ALTER TABLE memory.memories ENABLE ROW LEVEL SECURITY;
CREATE POLICY memories_tenant_isolation ON memory.memories FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Per-dimension vector table
CREATE TABLE memory.memory_vectors_1536 (
  memory_id UUID         PRIMARY KEY REFERENCES memory.memories(memory_id) ON DELETE CASCADE,
  tenant_id UUID         NOT NULL,
  scope     VARCHAR(20)  NOT NULL,
  scope_id  UUID         NOT NULL,
  embedding vector(1536) NOT NULL
);
CREATE INDEX idx_memory_vectors_1536_hnsw
  ON memory.memory_vectors_1536 USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

ALTER TABLE memory.memory_vectors_1536 ENABLE ROW LEVEL SECURITY;
CREATE POLICY memory_vectors_1536_tenant_isolation ON memory.memory_vectors_1536 FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- Same template added per dimension as new embedding models are onboarded.

-- Session ownership tracker (referenced by the session-scope rule).
-- Principal-bound (2026-06 fix): the old agent_id column broke the principal
-- abstraction this phase introduces — external api_key/app principals could
-- never own a session.
CREATE TABLE memory.sessions (
  session_id      UUID         PRIMARY KEY,
  tenant_id       UUID         NOT NULL,
  principal_type  VARCHAR(20)  NOT NULL,   -- 'agent' | 'api_key' | 'app'
  principal_id    UUID         NOT NULL,   -- resolved via the standard order:
                                           -- agent_id → api_key_id → app_id claim
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ  NOT NULL
);
CREATE INDEX idx_sessions_tenant_principal ON memory.sessions(tenant_id, principal_type, principal_id);
ALTER TABLE memory.sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY sessions_tenant_isolation ON memory.sessions FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> **`memory.sessions` row lifecycle (MANDATORY — closes the session-scope security gap):**
>
> Session rows are created EXPLICITLY by **xAgent** (Phase 9) the first time it accepts
> a task carrying the optional Contract-3 **`input.session_id`** field (that field is the
> producer — see the Phase 9 amendment) and the agent uses session-scope memory;
> registration is idempotent and happens BEFORE the first session-scope use.
> Memory-service does NOT lazy-create on first
> session-scope write — that would defeat the whole point of the session-ownership check
> (any caller passing any session_id would silently succeed).
>
> Endpoint (add to Component 1c — System Context):
> ```
> POST /v1/memories/sessions    Create session ownership row    ⚡
>   Auth: either auth mode — external agent JWT, or service-JWT (Contract 12)
>         + X-Forwarded-Agent-JWT
>   Body: { "session_id": "<uuid>", "expires_at": "2026-05-23T10:00:00Z" }
>   Server-side:
>     - tenant_id  = JWT.tenant_id
>     - (principal_type, principal_id) resolved from the JWT chain via the standard
>       order (agent_id → api_key_id → app_id claim); on the service-JWT path the
>       acting principal comes from `on_behalf_of` — reject 400 if the service JWT
>       has no `on_behalf_of`.
>     - INSERT ... ON CONFLICT (session_id) DO NOTHING (idempotent).
>     - Reject 409 if session_id exists with a DIFFERENT principal (cross-principal
>       session reuse).
>   Returns: { "session_id": "...", "principal_type": "...", "principal_id": "...",
>              "created": true | false }
> ```
> Sessions never expire from the table on their own — `expires_at` is informational for
> working-memory (📋). A nightly housekeeping CronJob may delete rows where
> `expires_at < NOW() - INTERVAL '30 days'` once working-memory is live; first cycle
> leaves them as audit history.

---

### Component 2 — Store Memory ⚡

**Two auth paths (identical to Phase 5):**
```
External (developer hitting the service from outside):
  Authorization: Bearer <agent-jwt>           ← verified by the service directly against
                                                Auth JWKS (compose parity — first cycle);
                                                Kong's JWT plugin in front is the cloud form

Internal (xAgent and other in-cluster callers):
  Authorization:         Bearer <service-jwt> ← Contract 12 service token
  X-Forwarded-Agent-JWT: <agent-jwt>          ← agent identity preserved
  traceparent:           00-<trace-id>-...    ← W3C, Contract 8

Identity (tenant_id, agent_id) is derived from the JWT chain — NEVER from the body.
agent_id / tenant_id / trace_id in the body → 400 (Contract 13 anti-pattern guard).
```

```json
POST /v1/memories

{
  "content":      "The user prefers concise bullet-point responses.",
  "memory_type":  "semantic",
  "scope":        "user",
  "scope_id":     "<opaque-user-id-within-tenant>",
  "importance":   0.8,
  "tags":         ["preference", "communication-style"],
  "metadata":     { "session_id": "<uuid>" }
}

Response:
{
  "memory_id": "<uuid>",
  "created_at": "2026-05-22T10:00:00.000Z"
}
```

**Server-side validation BEFORE write:**
1. `scope_id` ownership rule applied per HLD scope table:
   - `scope = "tenant"` → reject if `scope_id != JWT.tenant_id` (403).
   - `scope = "principal"` (legacy `agent`) → reject if `scope_id` != JWT-resolved principal_id AND caller lacks `platform:admin` (403).
   - `scope = "session"` → reject if no `memory.sessions` row with this session_id AND matching JWT-resolved `(principal_type, principal_id)` (403).
   - `scope = "user"`   → `scope_id` is opaque (no registry validation), but the write MUST record the JWT-resolved `principal_type`/`principal_id` (writer identity — the `principal_present` CHECK enforces it). Read visibility is governed by `memory.tenant_config.user_scope_visibility`: **default `principal_only`** (only the writing principal reads it back); `tenant_shared` is the legacy opt-in. There is NO unconditional tenant-shared behaviour.
2. `importance` clamped to `[0.0, 1.0]` (DB CHECK also enforces).
3. `content` size capped at 16 KiB; over-limit → `VALIDATION_ERROR`.

**On store:**
0. **Idempotency-Key short-circuit (BEFORE the paid embedding call):**
   - If `Idempotency-Key` header present, compute Valkey key
     `mem-idemp:{tenant_id}:{scope}:{scope_id}:store:{idempotency_key}`.
   - `SET ... NX EX 86400` with `status=in_flight`.
   - SET succeeds → proceed to step 1.
   - SET fails (`completed`) → return cached `{memory_id, created_at}` with header
     `Idempotent-Replay: true`. Skip steps 1–5 entirely; no embedding call, no dedup
     lookup, no row insert.
   - SET fails (`in_flight`) → return 409 `IDEMPOTENT_REQUEST_IN_FLIGHT` with
     `Retry-After: 2`.
   - Valkey outage → FAIL OPEN with telemetry (`memory_idempotency_skipped_total`),
     log WARN. The dedup-on-store at step 3 is the secondary defence; without
     Valkey it catches duplicate ROWS but not duplicate embedding charges.
1. Lazy-create `memory.tenant_config` row if absent (resolve `embed` alias to literal model + dim).
2. Embed content via LLMs Gateway `/v1/embeddings` using service JWT + `X-Forwarded-Agent-JWT`.
   Forward the inbound `X-Request-ID` and `traceparent`. Set
   `Idempotency-Key: mem-embed:{tenant_id}:{sha256(content)}` on the embedding call
   so a worker-crash + restart does not double-bill at the Phase 3 layer either.
3. Check for near-duplicate (cosine similarity > 0.95 with existing memory in same `(tenant_id, scope, scope_id)` — uses Phase 5's two-pass CTE pattern with `top_k = 1`).
4. **Duplicate semantics (deliberate, document explicitly):**
   - "Update" means: bump `importance` to `LEAST(existing + 0.1, 1.0)`, set `last_accessed_at = NOW()`, union `tags`. Content and embedding are NOT replaced (the original near-match wins; we do not pay a second embedding cost).
   - Do NOT replace content or embedding on dedup — implementers must not invent their own semantics.
5. Insert memory record with embedding (or skip insert + bump existing per step 4).
6. **Idempotency completion write:** if step 0 took the Valkey path, atomically
   update the same key to `status=completed` with the response body cached (gzip+base64,
   ≤4 KiB — memory responses are tiny so the size cap is generous). This must run
   AFTER the DB transaction commits so retries never replay a body whose row got rolled back.

---

### Component 2b — By-ID Access (GET / PUT / DELETE) ⚡

Three endpoints share one ownership check. RLS gates `tenant_id` automatically; the
scope-specific check below is APPLIED IN APPLICATION CODE after the SELECT.

```
GET    /v1/memories/{memory_id}    Read a single memory
PUT    /v1/memories/{memory_id}    Update mutable fields (importance, tags, metadata, ttl_at)
                                   Content + embedding are NEVER updated by this endpoint;
                                   replace = DELETE + POST (different memory_id).
DELETE /v1/memories/{memory_id}    Hard-delete (cascades to memory_vectors_*)
```

**Authorization flow (server-side, every endpoint, no exceptions):**

```
1. SELECT scope, scope_id, principal_type, principal_id FROM memory.memories WHERE memory_id = $1;
   -- RLS already gates tenant_id; rows from other tenants are invisible.
   -- If 0 rows → return 404 NOT_FOUND. (Do NOT distinguish "not found" from
   --   "cross-tenant" — that distinction is itself an information leak.)

2. Apply scope-ownership rule against the JWT, using the SAME table as Component 2 write path:
     scope = "tenant"    → allowed (RLS already proved tenant match).
     scope = "principal" → allowed iff memory.scope_id == JWT-resolved principal_id
       (legacy "agent")    OR caller has platform:admin scope.
     scope = "session"   → allowed iff
                           SELECT 1 FROM memory.sessions
                             WHERE session_id     = memory.scope_id
                               AND principal_type = <JWT-resolved principal_type>
                               AND principal_id   = <JWT-resolved principal_id>;
                           (i.e., session was minted for THIS principal).
     scope = "user"      → consult memory.tenant_config.user_scope_visibility:
                             'principal_only' (DEFAULT) → allowed iff
                                memory.principal_id == JWT-resolved principal_id
                                OR caller has platform:admin scope
                                OR a memory.user_scope_acl row grants this reader
                                   (Component 7d override);
                             'tenant_shared' (legacy opt-in) → allowed (RLS already
                                proved tenant match).

3. On mismatch → return 404 NOT_FOUND, NOT 403. Same anti-existence-leak rule
   Contract 15 test 4 enforces for cross-tenant access. A 403 would tell an
   attacker "the memory exists but isn't yours" — strictly worse than 404.

4. On success:
   - GET    → return the row.
   - PUT    → UPDATE mutable fields; reject any attempt to change scope, scope_id,
              content, embedding, embedding_model, embedding_dim with 400
              `VALIDATION_ERROR` (mutation of these requires DELETE + new POST).
   - DELETE → DELETE FROM memory.memories WHERE memory_id = $1
              (cascade drops memory_vectors_<N>). Emit no Kafka event — single-memory
              deletes are below the audit-event threshold; bulk wipe (Component 7)
              is the audit-emitting path.
```

> **Why this is its own Component:** Without an explicit by-ID flow, implementers
> tend to (a) rely on RLS alone and accidentally let any agent in a tenant read
> any other agent's memory by guessing IDs, or (b) invent their own 403 vs 404
> semantics. Both are bugs that look like they work in unit tests and fail in
> production audits. The single source-of-truth lookup + scope check above is the
> ONLY supported pattern.

---

### Component 3 — Semantic Retrieval ⚡

Auth uses the same two-path pattern as Component 2. `scope_id` ownership rules are enforced server-side BEFORE the vector query — a `scope = "agent"` retrieve with a non-matching `scope_id` returns 403, not "no results" (silent leak prevention).

```json
POST /v1/memories/retrieve

{
  "query":        "What does the user prefer for communication?",
  "scope":        "user",
  "scope_id":     "<opaque-user-id>",
  "top_k":        5,                    -- max 50 (server-enforced)
  "min_score":    0.7,
  "memory_types": ["semantic", "episodic"],
  "filters":      { "tags": ["preference"] },
  "ef_search":    100                   -- optional HNSW recall knob, cap 500
}

Response:
{
  "memories": [
    {
      "memory_id":   "<uuid>",
      "content":     "The user prefers concise bullet-point responses.",
      "memory_type": "semantic",
      "score":       0.92,
      "importance":  0.8,
      "tags":        ["preference", "communication-style"],
      "created_at":  "2026-05-10T09:00:00.000Z"
    }
  ],
  "duration_ms": 28
}
```

> **Retrieval SQL MUST use the two-pass CTE pattern documented in Phase 5 Component 4** (HNSW-friendly). A naive `WHERE 1 - (embedding <=> $vec) >= $min_score ORDER BY ...` repeats the Phase 5 anti-pattern — sequential scan instead of HNSW. The dedup-on-store lookup (Component 2 step 3) uses the same template with `top_k = 1`.

> **`last_accessed_at` writes** happen on every successful retrieve. At high QPS this is a hot per-row write. First-cycle: write inline (simple, correct). 📋: switch to async accessed-at tracker (per-pod batch UPDATE every 5s) once retrieve QPS justifies it.

---

### Component 4 — Memory Lifecycle (TTL & Expiry) ⚡

```
Background job (CronJob, runs every hour):
  -- BATCHED delete to avoid hour-long write-lock storms once data grows:
  LOOP
    DELETE FROM memory.memories
    WHERE memory_id IN (
      SELECT memory_id FROM memory.memories
      WHERE ttl_at < NOW()
      LIMIT 10000
    );
    -- exit loop when no rows deleted
    -- 1-second pause between batches; emit metric memory_ttl_swept_total + duration
  END LOOP

Default TTLs (per tenant_config — configurable per tenant):
  episodic:    NOW() + 90 days
  working:     NOW() + 24 hours (or session end, whichever is first)
  semantic:    no TTL (permanent)
  procedural:  no TTL (permanent)

Metrics: memory_ttl_swept_total{result=ok|error}, memory_ttl_sweep_duration_seconds.
Alert: backlog (rows-with-expired-ttl) > 100k for > 1h → operator pages on creeping leak.
```

> The previous draft used a single unbounded `DELETE FROM memory.memories WHERE ttl_at < NOW()`. At 10M+ rows with a sweep backlog (cluster restart, paused CronJob), that's an hour-long write lock that fights every other writer. Batched delete keeps lock duration bounded.

---

### Component 5 — Auto Memory Extraction 📋

**What it is:** After a conversation, automatically extract key facts to store as semantic memories.

```
POST /v1/memories/extract
Body: {
  "conversation": [
    { "role": "user",      "content": "..." },
    { "role": "assistant", "content": "..." }
  ],
  "scope":      "user",
  "scope_id":   "<user-uuid>",
  "agent_id":   "<uuid>"
}

Internally:
  1. Call LLMs Gateway with extraction prompt:
     "Extract key facts, preferences, and decisions from this conversation.
      Format as JSON array: [{ 'fact': '...', 'importance': 0.0-1.0, 'tags': [] }]"
  2. For each extracted fact:
     a. Check for existing duplicate (cosine similarity)
     b. If new: store as semantic memory
     c. If duplicate: update importance score
```

---

### Component 6 — Memory Summarisation 📋

**What it is:** Compress multiple episodic memories into a shorter semantic memory to reduce retrieval overhead.

```
POST /v1/memories/summarise
Body: {
  "scope":       "user",
  "scope_id":    "<user-uuid>",
  "memory_type": "episodic",
  "older_than":  "30d"
}

Internally:
  1. Fetch all episodic memories older than 30 days for this scope
  2. Call LLMs Gateway: summarise into key semantic facts
  3. Store summary as new semantic memory
  4. Archive (not delete) old episodic memories
```

---

### Component 7 — Bulk Wipe (GDPR) ⚡

GDPR Article 17 ("right to erasure") requires evidence the wipe happened. A hard DELETE with no audit row is not compliant — auditors and regulators ask for proof.

```sql
CREATE TABLE memory.gdpr_wipe_log (
  id                       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                UUID         NOT NULL,
  scope                    VARCHAR(20)  NOT NULL,
  scope_id                 UUID         NOT NULL,
  deleted_count            INTEGER      NOT NULL,
  requested_by_principal_id UUID        NOT NULL,
                           -- renamed from requested_by_agent_id (2026-06): derived from
                           -- the JWT chain in EITHER auth mode (principal abstraction)
  request_id               UUID         NOT NULL,
  requested_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  completed_at             TIMESTAMPTZ
);
CREATE INDEX idx_gdpr_wipe_tenant ON memory.gdpr_wipe_log(tenant_id, requested_at DESC);
ALTER TABLE memory.gdpr_wipe_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY gdpr_wipe_tenant_isolation ON memory.gdpr_wipe_log FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

```
DELETE /v1/memories?scope=user&scope_id=<opaque-user-id>
  requested_by_principal_id is derived from the JWT chain in EITHER auth mode:
    - external path: the agent JWT in Authorization (standard principal resolution);
    - internal path: X-Forwarded-Agent-JWT (or service-JWT on_behalf_of).
  External callers do NOT need — and cannot set — X-Forwarded-Agent-JWT (the old
  hard header requirement made the endpoint unusable from outside; fixed 2026-06).
  Header: X-Request-ID  (request_id derived from this; see note below)

  Server-side, in ONE transaction:
    SET LOCAL app.tenant_id = $JWT.tenant_id;
    1. INSERT INTO memory.gdpr_wipe_log (tenant_id, scope, scope_id, requested_by_principal_id,
                                         request_id, deleted_count=0);
    2. DELETE FROM memory.memories WHERE tenant_id=$t AND scope=$s AND scope_id=$sid
       RETURNING memory_id;                          -- cascade drops memory_vectors_*
    3. UPDATE memory.gdpr_wipe_log SET deleted_count=$n, completed_at=NOW() WHERE id=$wid;
    4. INSERT INTO memory.outbox (topic, partition_key, payload) VALUES
       ('cypherx.memory.gdpr.wiped', tenant_id::text, <Contract 5 envelope JSON>);
  COMMIT;

  Returns: { "deleted_count": 47, "wipe_log_id": "<uuid>" }
```

> **`request_id` and `trace_id` provenance (MANDATORY — same rule as Phases 3/4/5):**
> - `request_id` = value of the inbound `X-Request-ID` header. Kong's `correlation-id`
>   plugin injects this on every external request and callers forward it on internal
>   hops. The service MUST NOT mint its own `request_id` when the header is present.
>   If absent (internal-only call path that bypassed Kong), generate a UUIDv4, set
>   `X-Request-ID` on any outbound calls, and emit a WARN log
>   `request_id_generated_fallback=true`.
> - `trace_id` (in the Kafka envelope) = 16-byte ID parsed from `traceparent`. If
>   `traceparent` is absent, synthesise + WARN, never publish NULL.
> - Both fields are taken ONLY from headers — never from the request body. Body field
>   named `request_id` or `trace_id` → 400 `VALIDATION_ERROR`.
> - Carrying `request_id` on the wipe row makes a GDPR-audit query trivial:
>   "show me every system action triggered by request X" joins `memory.gdpr_wipe_log`
>   ↔ `llms.usage_records` ↔ `guardrails.violations` ↔ `rag.documents` on a single
>   field, instead of guessing by timestamp window.

Kafka event `cypherx.memory.gdpr.wiped` is published via the outbox (Component 7b below) so the audit pipeline cannot miss it.

---

### Component 7b — Transactional Outbox ⚡ (NEW)

Required for the GDPR wipe event above and any future memory Kafka events. Same divergence-prevention rationale as Phases 3/4/5: a wipe row + a missing Kafka event = compliance gap that auditors love to find.

```sql
CREATE TABLE memory.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,        -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,        -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX idx_outbox_unpublished
  ON memory.outbox(created_at) WHERE published_at IS NULL;
-- Platform-internal table — no RLS (only memory-service writes; reader is the same service).
```

Publisher loop: one goroutine per pod, batch SELECT 100, publish with `partition_key`, mark `published_at`, exponential backoff on failure, DLQ to `<topic>.dlq` after 10 attempts. Nightly job deletes rows where `published_at < NOW() - INTERVAL '7 days'`.

Topics produced (first cycle: provisioned via the idempotent `topics-init` compose job — `rpk topic create`, safe to re-run; Phase 6 Terraform is the cloud form, see Compose-Parity Runtime subsection):
- ⚡ `cypherx.memory.gdpr.wiped` — `{wipe_log_id, tenant_id, scope, scope_id, deleted_count, requested_by_principal_id, request_id, trace_id}`
- ⚡ `cypherx.memory.usage.recorded` — see Component 7e (every store, retrieve, extraction). Never sampled.
- 📋 `cypherx.memory.stored` — high-importance write digest (importance ≥ 0.8 or first per (scope, scope_id)). Opt-in for downstream analytics.

DLQ topics provisioned per Phase 1 Component 17 convention.

---

### Component 7e — Usage Metering (Contract 19) ⚡ (NEW — renumbered from the duplicate "7b"; see Amendment Log)

Every billable memory operation emits one event on `cypherx.memory.usage.recorded` via the same outbox transaction. Without this, Memory cannot be priced standalone.

| Operation | `units` payload | Cost driver |
|-----------|-----------------|-------------|
| `store` | `{ embedding_tokens, bytes_stored, importance }` | embedding LLM cost + storage |
| `retrieve` | `{ top_k, bytes_scanned, embedding_tokens (query) }` | compute + embedding for query |
| `extract` | `{ llm_tokens_used }` + cross-link to LLMs usage event via `request_id` | LLM extraction |
| `summarise` | `{ llm_tokens_used }` + cross-link via `request_id` | LLM summarisation |
| `forget` (GDPR) | `{ deleted_count }` | zero-cost, emitted for compliance auditing |

```
{
  "tenant_id":      "<uuid>",
  "api_key_id":     "<uuid|null>",
  "agent_id":       "<uuid|null>",
  "principal_type": "agent|api_key|app",
  "principal_id":   "<uuid>",
  "scope":          "tenant|principal|user|session",
  "scope_id":       "<uuid>",
  "operation":      "store|retrieve|extract|summarise|forget",
  "units":          { ... },
  "cost_usd":       0.0000123,
  "duration_ms":    18,
  "request_id":     "<uuid>",
  "trace_id":       "<uuid>"
}
```

Cost calculation references `memory.pricing` (admin-managed) with per-tenant overrides in `memory.tenant_pricing`. Embedding costs are NOT double-billed — the LLMs gateway's `usage.recorded` event is the source of truth for embedding spend; Memory's event carries only the cost-of-storage component plus a `request_id` join key. Billing rollup (Phase 11) de-duplicates on `request_id`.

---

### Component 7c — Pluggable Vector Storage 📋 (NEW design / ⚡ interface)

Same pattern as RAG Component 5e: ship the `IVectorStore` interface in first cycle with a `PgVectorAdapter` only. `memory.tenant_backends (tenant_id, backend_type, connection_ref, config)` lets enterprise customers BYO Pinecone/Qdrant later without query-layer rewrites. The interface is ⚡ first-cycle; concrete non-pgvector adapters are 📋.

---

### Component 7d — Per-User ACL on User-Scope Memories ⚡ (NEW)

Per the audit fix, `user_scope_visibility` now defaults to `principal_only` for new tenants. For tenants that want intra-tenant cross-agent sharing on user-scope (the legacy default), the per-tenant config flag must be flipped to `tenant_shared`.

**Data Model addition:** `memory.tenant_config.user_scope_visibility VARCHAR(20) NOT NULL DEFAULT 'principal_only' CHECK (user_scope_visibility IN ('principal_only', 'tenant_shared'))`.

**Optional finer-grained ACL** (📋 — enterprise customers wanting "agent A can read user X's memories but agent B cannot"):

```sql
CREATE TABLE memory.user_scope_acl (
  tenant_id       UUID NOT NULL,
  user_scope_id   UUID NOT NULL,
  reader_principal_type VARCHAR(20) NOT NULL,
  reader_principal_id   UUID NOT NULL,
  permissions     TEXT[] NOT NULL,  -- read | write | forget
  PRIMARY KEY (tenant_id, user_scope_id, reader_principal_type, reader_principal_id)
);
ALTER TABLE memory.user_scope_acl ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_user_scope_acl ON memory.user_scope_acl
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

When this table has rows for a `(tenant_id, user_scope_id)`, the table overrides the tenant-wide `user_scope_visibility` flag for that user.

---

### Component 8 — Working Memory 📋

**What it is:** Per-session context window extension stored temporarily in Valkey.

```
Key pattern: working-mem:{session_id}:{tenant_id}
TTL: 24 hours

Operations:
  POST /v1/memories/working/append  → append to session context
  GET  /v1/memories/working/{session_id} → get full session context
  DELETE /v1/memories/working/{session_id} → clear session
```

---

### Compose-Parity Runtime (first cycle — AMENDED, see Amendment Log)

The first-cycle runtime is **docker compose + Neon (Postgres) + Valkey + Redpanda + MinIO**.
There is NO K8s, Kong, Istio, Doppler, AWS, or Argo in the first cycle. The K8s spec below is
the **deploy-target (cloud) form**, conditional on the infra phase. Compose equivalents:

- **Service:** one `memory-service` compose service; same image, env-driven config; `/livez` /
  `/readyz` wired as compose `healthcheck`s (startup grace via `start_period: 60s` — the
  startupProbe stand-in). The K8s resource sizing below documents the cloud form; compose
  needs no resource spec first cycle.
- **External auth:** no Kong — the service verifies external agent JWTs DIRECTLY against
  Auth JWKS (`AUTH_JWKS_URL`); Kong's JWT plugin in front is the cloud form. The internal
  path (service JWT + `X-Forwarded-Agent-JWT`) is identical in both forms.
- **Kafka topics:** an idempotent `topics-init` compose job (`rpk topic create` against
  Redpanda, safe to re-run) provisions `cypherx.memory.usage.recorded`,
  `cypherx.memory.gdpr.wiped` + DLQ pairs — the Terraform stand-in. Topic names/partitions
  identical.
- **Config/secrets:** every env var below is supplied via compose `.env` / environment
  blocks ("from Doppler" is the cloud form). `AUTH_*` / `LLMS_GATEWAY_URL` point at compose
  service DNS (e.g. `http://auth:8080`) instead of cluster DNS.
- **Scheduled jobs** (batched TTL expiry): cron sidecar or CI scheduled pipeline first
  cycle; K8s CronJob in the cloud form. Same batch/pause/metrics semantics.

### K8s Deployment Spec (deploy-target / cloud form — conditional on the infra phase)

```yaml
Namespace:   shared-core
Deployment:  memory-service
Replicas:    min 2, max 8 (HPA on CPU 70% — first-cycle minimum)
Node selector: node-role: core

Resources:
  requests: { cpu: 500m, memory: 512Mi }
  limits:   { cpu: 1000m, memory: 1Gi }
  # Bumped from 200m/256Mi/1000m/512Mi: memory service does HNSW queries,
  # HTTP embedding round-trips, and dedup vector lookups — the prior 512Mi
  # limit would OOM under modest load.

Startup probe (Postgres + pgvector + tenant_config seed lookup):
  startupProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    failureThreshold: 12          # 60s grace

Health probes (Contract 7):
  livenessProbe:
    httpGet: { path: /livez, port: 8080 }
    periodSeconds: 10
    # Process-only — NEVER touches DB / LLMs / Valkey / Kafka.
  readinessProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    # Hard deps (fail readiness):
    #   - PostgreSQL reachable
    #   - pgvector extension present
    # Soft deps (log + metric only):
    #   - Valkey (working memory, future)
    #   - Kafka  (outbox keeps events durable until publisher reconnects)
    #   - LLMs gateway (only needed for store/retrieve, not health)

Env vars (env-driven — compose `.env` first cycle; Doppler-injected in the cloud form):
  DATABASE_URL                 (PgBouncer → memory schema, runtime user mem_user)
  VALKEY_URL                   (soft dep)
  KAFKA_BROKERS
  KAFKA_SASL_PASSWORD
  AUTH_SERVICE_URL             (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET     (Contract 12; from service-auth/memory-service/bootstrap_secret)
  LLMS_GATEWAY_URL             (http://llms-gateway.shared-core.svc.cluster.local:8080)
```

> **Service ACL (cross-phase Phase 2 update):** Phase 2's `auth.service_acl` seed
> must be extended when Phase 6 deploys:
> - `memory-service → llms-gateway [internal:read]` (embeddings)
> - `memory-service → auth-service [internal:read]` (service-token mint + JWKS)
>
> Phase 6's Atlas migration ships these as idempotent `INSERT ... ON CONFLICT DO NOTHING`
> against `auth.service_acl` (allowed because the migration runs with platform-admin DDL credentials).
>
> **JWKS verification** follows the Phase 3 pattern: in-cluster URL only, 5-minute cache,
> refresh-on-`kid`-miss rate-limited to 1/min.

---

## ⚡ First Cycle Implementation Checklist

- [ ] Service architecture planned separately
- [ ] **`memory.tenant_config`** table created; lazy row-creation resolves `embed` alias to literal model + dim, persisted immutably
- [ ] Store memory endpoint (`POST /v1/memories`) — episodic + semantic types
- [ ] **Idempotency-Key on `POST /v1/memories` short-circuits BEFORE the embedding call** — Valkey `mem-idemp:...`, 24h TTL, replay returns cached body with `Idempotent-Replay: true`; fail-open on Valkey outage. Embedding call forwards deterministic `Idempotency-Key: mem-embed:{tenant}:{sha256(content)}` to LLMs gateway.
- [ ] **scope_id ownership enforced server-side** (tenant: must=JWT.tenant_id; principal/agent: must=JWT-resolved principal_id; session: tracked in `memory.sessions`, principal-bound; user: respects `memory.tenant_config.user_scope_visibility` flag — `principal_only` by default, `tenant_shared` legacy opt-in — on BOTH the store path (Component 2) and the by-ID/read path (Component 2b))
- [ ] **`principal` scope alias for `agent`** — `principal_type` + `principal_id` columns; legacy `scope=agent` accepted and rewritten at write time; new code uses `principal`
- [ ] **`user_scope_visibility` defaults to `principal_only` for new tenants** (Component 7d); CHECK constraint enforces enum; migration sets `tenant_shared` for tenants that existed before this change
- [ ] **Usage metering (Component 7e) ⚡** — `cypherx.memory.usage.recorded` on store/retrieve/extract/summarise/forget via outbox; events carry **units + `request_id` ONLY** (no cost fields, no cross-schema joins to llms pricing); `memory.pricing` table + per-tenant override; embedding cost de-duplicated against LLMs usage events on `request_id` DOWNSTREAM at billing rollup (single-owner rule — see Amendment Log)
- [ ] **Storage abstraction (Component 7c) ⚡ interface** — `IVectorStore` defined; `PgVectorAdapter` is the only impl in first cycle; `memory.tenant_backends` seeded `pgvector` for every tenant
- [ ] **Memory quotas enforced** — `memories_max`, `storage_bytes_max`, `stores_per_min`, `retrieves_per_min` from `auth.tenant_quotas`; 413 `QUOTA_EXCEEDED` on storage cap
- [ ] **`scope_id` UUID-mapping documentation** — caller MUST map non-UUID native user IDs via `uuid5(tenant_id, native_id)`; no server-side translation
- [ ] **`POST /v1/memories/sessions` endpoint** — xAgent creates session ownership rows BEFORE writing session-scope memories (producer: Contract-3 `input.session_id`, see Phase 9); idempotent on conflict; rows bind `(principal_type, principal_id)`, not `agent_id`; rejects cross-principal reuse with 409
- [ ] **By-ID access (Component 2b)** — GET/PUT/DELETE share one ownership-check flow; mismatch returns **404** (anti-existence-leak), not 403; PUT rejects mutation of scope/scope_id/content/embedding/embedding_model/embedding_dim with `VALIDATION_ERROR`
- [ ] **`global` scope renamed to `tenant`** in code + migration; CHECK constraint enforces enum
- [ ] **`importance` CHECK constraint** (0.0–1.0); dedup bump clamped via LEAST
- [ ] **Dedup semantics fixed** — bump importance/last_accessed_at/union tags ONLY; content + embedding never replaced
- [ ] **content size cap** 16 KiB; over-limit → `VALIDATION_ERROR`
- [ ] Semantic retrieval endpoint (`POST /v1/memories/retrieve`); `top_k` capped 50; `ef_search` knob capped 500
- [ ] **Retrieval SQL uses two-pass CTE pattern** (Phase 5 Component 4); same for dedup lookup
- [ ] Get / Update / Delete by ID with same ownership enforcement
- [ ] **Bulk wipe via `DELETE /v1/memories?scope=...&scope_id=...` writes to `memory.gdpr_wipe_log` and emits `cypherx.memory.gdpr.wiped` via outbox in one transaction**; `requested_by_principal_id` derived from the JWT chain in EITHER auth mode (no `X-Forwarded-Agent-JWT` requirement for external callers); `request_id` from `X-Request-ID` header (provenance rule mirrors Phases 3/4/5); fallback-synth + WARN if absent; never accepted from body
- [ ] **pgvector metadata + per-dimension `memory_vectors_<N>` tables, HNSW index** (mirrors RAG pattern)
- [ ] **GIN indexes** on `metadata` (jsonb_path_ops) and `tags`
- [ ] **`memory.sessions`** table tracking session ownership for session-scope checks — principal-bound (`principal_type` + `principal_id`); rows are created ONLY by `POST /v1/memories/sessions` (never lazy-created on first memory write)
- [ ] **Batched TTL expiry** scheduled job (10k rows per batch, 1s pause, metrics + alert on backlog) — cron sidecar / CI schedule first cycle (K8s CronJob is the cloud form)
- [ ] **RLS on all `memory.*` tables** — tenant isolation per Contract 13
- [ ] **Two auth paths** — external (agent JWT verified DIRECTLY by the service against Auth JWKS — compose parity; Kong-fronted JWT verification is the cloud form) + internal (service JWT + X-Forwarded-Agent-JWT)
- [ ] **`AUTH_JWKS_URL` + `SERVICE_BOOTSTRAP_SECRET`** env vars (Phase 3 JWKS pattern)
- [ ] **Service ACL extension** via migration (`memory-service → llms-gateway`, `memory-service → auth-service`)
- [ ] **`memory.outbox` table + publisher loop + DLQ after 10 attempts**
- [ ] **Kafka topic `cypherx.memory.gdpr.wiped` + DLQ** provisioned via the idempotent `topics-init` compose job (`rpk topic create`) first cycle — Phase 6 Terraform is the cloud form
- [ ] Response field `duration_ms` (not `latency_ms`) — cross-service consistency
- [ ] Atlas migrations (Contract 14) for `memory.*` schema (tenant_config, memories, memory_vectors_<N>, sessions, gdpr_wipe_log, outbox)
- [ ] **`/livez`, `/readyz`, `/metrics`** endpoints; readiness gated on Postgres + pgvector
- [ ] **Startup grace** configured (60s) — compose `healthcheck` `start_period: 60s` first cycle (K8s startupProbe is the cloud form)
- [ ] Resource sizing documented (500m/512Mi req, 1000m/1Gi lim — K8s cloud form; compose needs no resource spec first cycle)
- [ ] Runs as a compose service with `/livez` / `/readyz` wired as compose healthchecks (compose parity — see Compose-Parity Runtime subsection; deploy to K8s via ArgoCD is the cloud form, conditional on the infra phase)

## 📋 Full Enterprise Implementation Checklist

- [ ] Auto memory extraction from conversation (`POST /v1/memories/extract`) — LLM-cost discipline required
- [ ] Memory summarisation / consolidation
- [ ] Working memory (Valkey-backed, session-scoped; loss-on-restart accepted by design)
- [ ] Importance scoring and decay
- [ ] Memory consolidation background job (episodic → semantic)
- ("Memory quota per tenant/user/agent" 📋 item DELETED 2026-06 — duplicate of the ⚡ "Memory quotas enforced" item; quota ENFORCEMENT is single-owned by Phase 6 in the first cycle, and Phase 13 Domain 3 only TUNES limit values. See Amendment Log)
- [ ] Time-based retrieval ("what happened last week")
- [ ] Manual memory review API (for UI)
- [ ] Per-principal ACL on user-scope memories (`memory.user_scope_acl`, Component 7d — finer-grained override of the `user_scope_visibility` flag, whose default is `principal_only`)
- [ ] Async `last_accessed_at` tracker (per-pod batch UPDATE every 5s) — once retrieve QPS justifies
- [ ] Re-embed background job (post tenant_config embedding-model change)
- [ ] Kafka event: `cypherx.memory.stored` on significant memory writes (importance ≥ 0.8 or first per scope)
- [ ] Memory usage metrics (storage size, retrieval latency, dedup hit rate)

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Memory Service as a Synchronous Critical Path — REAL
Evidence: lines 25–28, 280–283. Store requires sync embedding call; retrieve always sync vector query.
**Mitigation:** on LLMs-gateway timeout (>2 s), cache write in Valkey with temporary embedding, return 202 with retry-after, re-embed async. Emit `embeddings_async_fallback_total`.

### 2. Session Ownership Model Limitation for Multi-Agent Collaboration — PARTIAL
Evidence: lines 180–186, 193–216 (sessions bound to single agent; cross-agent reuse = 409).
**Mitigation (future):** optional `shared_principals[]` on `POST /v1/memories/sessions`; retrieve permitted across listed principals; writes remain isolated per agent.

### 3. Retrieval Hotspot Risk — REAL
Evidence: line 389 (per-row `last_accessed_at` write on every retrieve).
**Mitigation:** when per-pod retrieve QPS >100/min, switch to per-pod batch UPDATE every 5 s; emit `accessed_at_batch_updates_total`.

### 4. Cross-Model Embedding Migration Complexity — REAL
Evidence: lines 91–108 (model pinned per tenant; no migration path).
**Mitigation:** admin `POST /v1/admin/memory/reembed` triggers async re-embed; progress in `memory.tenant_config.reembed_job_id`; new writes blocked on tenant during reembed window; emit `reembed_duration_seconds`.

### 5. Dedup Similarity Threshold Rigidity — REAL
Evidence: line 284 (hardcoded `cosine > 0.95`).
**Mitigation:** add `memory.tenant_config.dedup_similarity_threshold FLOAT NOT NULL DEFAULT 0.95 CHECK (>= 0.7 AND <= 1.0)`; Store uses configured value.

### 6. Potential RLS + pgvector Scaling Pain — REAL
Evidence: lines 157–175 (RLS on memories + per-dim vector tables).
**Mitigation:** benchmark HNSW under RLS; if sequential-scan fallback observed, document in runbook and emit `rls_sequential_scan_total`. Track index-assisted RLS (PostgreSQL 17+) for future migration.

### 7. Missing Explicit Circuit Breakers — REAL
Evidence: lines 276–278 (Valkey idempotency fails-open; no breaker on LLMs gateway or PG).
**Mitigation:** circuit breaker on LLMs-gateway embedding — open after 3 consecutive >2 s timeouts, hold 30 s; emit `embedding_circuit_breaker_open`; readiness stays green (soft-fail); queue failed requests in Valkey for async retry.

### 8. Principal Abstraction — VERIFIED (strong decision)
Evidence: lines 52–54. Explicit rename to support external vendors.

### 9. Tenant Embedding Pinning — VERIFIED (strong decision)
Evidence: lines 91–108. Immutability enforced in schema.

### 10. GDPR Audit + Transactional Outbox — VERIFIED (strong decision)
Evidence: lines 473–562. Wipe log + transactional outbox + request_id provenance.
