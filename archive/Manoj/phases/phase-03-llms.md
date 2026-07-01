# Phase 3 — SharedCore / LLMs Gateway
> **Status:** ⏳ Pending | **Depends On:** Phase 0, 1, 2 | **Blocks:** Phase 4, 9
> **First Cycle:** ⚡ Partial — unified completions endpoint with 2 providers required

## Amendment Log (2026-06 — pre-build reconciliation)

- **BYOK secret backend made pluggable (Component 8 + `llms.providers` DDL).** The AWS-Secrets-Manager/Doppler-only `key_ref` CHECK constraint was unimplementable in the actual compose/Neon runtime (no AWS, no Doppler). Replaced with a registry-driven backend table (`llms.secret_backends`); first-cycle backends are `sealed:v1:<uuid>` (raw key AES-256-GCM envelope-encrypted in Postgres under an env-supplied KEK — same pattern Auth uses for signing keys) and `env:<VAR>` for platform keys. `secretsmanager:` is retained as an optional future backend (cloud form), disabled in the registry.
- **Billing uniqueness key changed to gateway-minted `llm_call_id` (Component 4).** `UNIQUE(tenant_id, request_id)` + hot-path `ON CONFLICT DO NOTHING` silently dropped the billing row for any 2nd completion under one forwarded `X-Request-ID` while still emitting both Kafka events. Now: per-call `llm_call_id` UUID is the uniqueness key; `request_id` is a non-unique correlation column; the hot-path INSERT fails loudly on duplicates; only the billing-replay worker uses `ON CONFLICT`; Kafka payloads carry both ids. Callers change nothing.
- **Tenant plan tier interim-owned by Auth (Component 5).** Plan (free/pro/enterprise) was owned by deferred px0/platform-mgmt with no source anywhere. Auth now carries `tenants.plan` (default `'free'`), surfaced as a JWT claim AND via Auth's limits endpoint; gateway caches it 60 s in Valkey. Later migration to platform-mgmt is documented and does not change the gateway contract.
- **De-hardcoding (Components 1/2/5 + seeds).** `llms.model_capabilities` pulled into Phase 3 (was Phase 13); DB is the single authority for aliases/pricing/capabilities with startup-blocking load + 60 s periodic refresh; in-code maps are demoted to documented test fixtures (never consulted at runtime). Seeds reconciled per audit: opus model id drift (`claude-opus-4-7` → `claude-opus-4-8`), `embed`/`code`/`vision` aliases present in the seed migration itself, embedding pricing rows added.
- **Stream + idempotency semantics decided (Components 4/6).** `stream=true` requests RECORD the Idempotency-Key but are replay-exempt — duplicates re-execute and respond with `Idempotent-Replayed: false`. Error-code spelling aligned to Contract 2 (`IDEMPOTENCY_*`).
- **Rate-limiting semantics decided (Component 5).** Token pre-check is impossible (completion size unknown before dispatch): `requests_per_min` is the pre-flight check; `tokens_per_min` is a post-hoc window debit from actual usage; predictive token limiting moves to 📋.
- **`GET /v1/usage` + `GET /v1/cost` are ⚡ only.** They appeared in BOTH checklists; the 📋 duplicate is deleted — 📋 keeps only dashboard extensions on top of the ⚡ aggregation endpoints.
- **No second `api_keys` table.** Auth owns API keys; LLMs stores ONLY `llms.api_key_acls` keyed by Auth's `api_key_id` (⚡ checklist corrected).
- **WP06 pre-work package declared (gates Phases 5/6).** Embeddings (256-item/25 MiB caps, mock provider, usage+outbox), `GET /v1/models` with `embedding_dim`, `embed` alias + embedding pricing seeds, and Valkey Idempotency-Key replay are an explicitly named dependency of Phases 5 (RAG) and 6 (Memory) — those builds may not start ahead of it.
- **Compose-parity (deployment section + checklist split).** First-cycle runtime is compose + Neon + Valkey + Redpanda + MinIO — no K8s/Kong/Istio/Doppler/AWS/Argo. A Compose-Parity subsection documents the runtime equivalents; the ⚡ checklist is split into "service code" (compose-buildable) vs "deploy-target" (cloud form, conditional on the infra phase). An idempotent `topics-init` compose job (`rpk topic create`) stands in for Terragrunt-provisioned topics.
- **Minor batch: SSRF fetcher premise stale (Component 1).** The "gateway runs inside the VPC" premise does not hold in the compose runtime. Multimodal `image_url` defaults to URL pass-through to the provider; the SSRF-hardened fetcher is config-gated (off by default), full hardening spec retained for when it is enabled (cloud form).
- **Header spelling harmonized to the registry: `Idempotent-Replayed` (canonical — Contract 9 + `contracts/http/headers.md`).** All occurrences of the drifted `Idempotent-Replay` spelling in this doc (Component 4 replay/over-cap flow, stream semantics, ⚡ checklist) corrected. No semantic change.

---

## Phase Overview

SharedCore/LLMs is the **unified LLM gateway**. No service in the platform ever calls an LLM provider directly — all LLM traffic flows through this gateway. This enables provider abstraction, cost tracking, rate limiting, caching, and routing in one central place.

**Deliverable:** A running LLM gateway that accepts a single unified request format and routes to Anthropic and OpenAI (first cycle), with token tracking, basic rate limiting, and streaming support.

> 🏗️ **Service Architecture Note:** The internal architecture of the LLMs gateway (language, framework, provider client libraries, streaming implementation, request queue design, caching layer internals) must be planned separately before implementation begins.

> ⚠️ **Coupling watch — gateway aggregates eight stateful subsystems on one pod and one Valkey:** idempotency store, semantic response cache, rate-limit counters, outbox publisher, BYOK secret cache, JWKS cache, tenant-plan cache, budget tracker. Acceptable at first cycle (single deployment unit, simple ownership) but it is coupling debt. 📋 Phase 13 hardening MUST evaluate extracting `budget tracker` + `provider-pricing` into a sibling `llms-billing` service (they read Kafka, not the hot path) so the gateway stays focused on request normalization + provider proxying. Until that split happens, treat every subsystem added here as a blast-radius multiplier — review before adding the ninth.

---

## High Level Design

### System Context

```
                             ┌──────────────────────────────────────────┐
                             │           LLMs GATEWAY                   │
                             │                                          │
  xAgent  ──────────────────►│  POST /v1/chat/completions               │
  Guardrails ────────────────│  POST /v1/embeddings                     │
  RAG (embeddings) ──────────│  GET  /v1/models                         │
  Memory (embeddings) ───────│  GET  /v1/usage                          │
                             │  POST /v1/keys (BYOK)                    │
                             └──────────────┬───────────────────────────┘
                                            │
               ┌───────────────────────────┼────────────────────────────┐
               ▼                           ▼                            ▼
          Anthropic API              OpenAI API                 Other Providers
          (claude-*-*)              (gpt-4o, etc.)             (Gemini, Groq, etc.)
```

### Provider Abstraction Model

```
LLMs Gateway receives unified request
  │
  ▼
Request Normaliser
  │  Unified format → Provider-specific format
  ▼
Router
  │  Pick provider based on: model alias / routing policy / fallback rules
  ▼
Provider Adaptor (Anthropic | OpenAI | ...)
  │  Translates request, calls provider API, translates response back
  ▼
Response Normaliser
  │  Provider-specific response → Unified response format
  ▼
Post-processing (token counting, cost calculation, usage recording)
  │
  ▼
Return to caller (streaming or non-streaming)
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first. 📋 items design now, implement after first cycle.

---

### Component 1 — Unified Request/Response Schema ⚡

The unified schema must be a **superset** of every provider's request/response shape — agents write to one schema, the gateway translates per-provider. Critically, this includes **tool-use** (function calling) since xAgent's execution loop depends on it.

**Unified Chat Completion Request:**

> **Authentication modes (the gateway accepts both):**
> - **Internal-caller mode (xAgent, internal services):** `Authorization: Bearer <service-jwt>` + `X-Forwarded-Agent-JWT: <agent-jwt>` (Contract 12 pattern). Gateway uses the service JWT to authenticate the caller and the forwarded agent JWT to derive `tenant_id`/`agent_id` for billing/RLS.
> - **External-caller mode (third-party developer):** `Authorization: Bearer <agent-jwt OR api-key-exchanged-jwt>` only, with NO `X-Forwarded-Agent-JWT`. Issuer of the bearer MUST be `AUTH_ISSUER_URL` (no `svc:*` sub allowed). Gateway derives `tenant_id`/`api_key_id`/`agent_id` from the bearer directly. This is the path external developers take via the public ingress (Kong in the cloud form; in the compose runtime, the gateway's published port directly).
> - Both modes feed the SAME downstream code path. The auth middleware sets `app.tenant_id` from whichever JWT carries it; the rest of the request handling does not branch.

```json
POST /v1/chat/completions
Authorization: Bearer <jwt>                    // service-jwt (internal) OR agent/api-key jwt (external)
X-Forwarded-Agent-JWT: <agent-jwt>             // OMITTED in external-caller mode

{
  "model":       "claude-sonnet-4-6",       // model alias or literal model ID
  "messages": [
    { "role": "system",    "content": "You are a helpful assistant." },
    { "role": "user",      "content": [
        { "type": "text",       "text": "What is in this image?" },
        { "type": "image_url",  "image_url": { "url": "https://...", "detail": "auto" } }
    ]},
    { "role": "assistant", "content": null,
      "tool_calls": [
        { "id": "call_1", "type": "function",
          "function": { "name": "web_search", "arguments": "{\"query\":\"...\"}" } }
      ]
    },
    { "role": "tool", "tool_call_id": "call_1", "content": "<tool result JSON>" }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name":        "web_search",
        "description": "Search the web",
        "parameters":  { "type": "object", "properties": { "query": { "type": "string" } }, "required": ["query"] }
      }
    }
  ],
  "tool_choice": "auto",                    // auto | none | required | { "type": "function", "function": { "name": "..." } }
  "max_tokens":  2048,
  "temperature": 0.7,
  "stream":      false,
  "stream_options": {                       // server-honoured streaming flags (present even when stream=false; ignored if so)
    "include_usage":         true,          // forced true on OpenAI streaming calls; unified final event always carries usage
    "aggregate_tool_calls":  true           // first-cycle default; `false` reserved for future delta passthrough (Phase 12)
  },
  "parallel_tool_calls": true,              // unified flag; Anthropic adaptor maps to `disable_parallel_tool_use: !this`
  "response_format": { "type": "text" },    // text | json_object | { "type": "json_schema", "json_schema": {...} }
  "metadata": {                              // free-form client tags ONLY
    "user_label": "research-loop-step-3"
  }
}
```

> **Identity is JWT-derived, never body-derived (Contract 13 anti-pattern):**
> `agent_id` and `tenant_id` are extracted from `X-Forwarded-Agent-JWT`. `trace_id` and `span_id` are extracted from the `traceparent` header. `request_id` is extracted from the `X-Request-ID` header. The `metadata` field is for free-form client tags only — it MUST NOT contain any of the reserved identity/correlation keys: **`agent_id`, `tenant_id`, `trace_id`, `span_id`, `request_id`, `task_id`, `user_id`, `org_id`** (the last two are reserved aliases that clients sometimes use instead of the canonical names). Gateway rejects with 400 `VALIDATION_ERROR` (`reserved_metadata_key`) if any of these appear in `metadata`. The reserved-set list lives in `contracts/api/reserved-metadata-keys.md` so every service applies the same rule.

> **Body size:** override of Contract 9's 1 MiB default. `POST /v1/chat/completions` and `POST /v1/embeddings` accept up to **25 MiB** to allow base64-encoded multimodal payloads. Declared in OpenAPI; the gateway enforces the cap itself (Kong route honors it in the cloud form — no Kong in the compose runtime).

> **Multimodal `image_url` handling (amended — see Amendment Log; the "gateway runs inside the VPC" SSRF premise is stale for the compose runtime):** Default behavior is **URL pass-through** — the gateway forwards the `image_url` to the provider unmodified and never fetches it itself (providers fetch the URL on their side; no gateway-side SSRF surface exists). A **config-gated fetcher** (`MULTIMODAL_FETCHER_ENABLED`, default `false`) re-enables gateway-side fetch + base64 inlining for providers/deployments that require inlined images (cloud form). When — and only when — the fetcher is enabled, it MUST implement the full SSRF-hardening ruleset below (Contract 3 baseline + extensions specific to this proxy):
> - **Scheme allowlist:** HTTPS only. `http://`, `file://`, `data:`, `ftp://`, `gopher://`, etc. all rejected at URL parse.
> - **TLS:** full chain validation against system trust store. No `InsecureSkipVerify`. SNI required. Hostname verification mandatory.
> - **Resolve-once + IP pin:** DNS lookup happens ONCE; the HTTP client connects using the resolved IP literal with `Host` and SNI set to the original hostname. Defeats DNS rebinding — a second lookup between the safety check and TCP connect cannot redirect us at an internal address.
> - **IP denylist on resolved A/AAAA records** before connect: 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16 (cloud metadata: AWS/GCP/Azure IMDS), ::1, fc00::/7, fe80::/10, 100.64/10 (CGNAT), 0.0.0.0/8, multicast (224/4, ff00::/8). If ANY resolved record falls in the denylist, the entire fetch is rejected (Happy-Eyeballs does not get to retry with an internal IPv6).
> - **Redirects:** followed up to 3 hops; IP-denylist + DNS-pin check re-runs on every hop. Redirect to a denylisted IP fails the request.
> - **Size cap:** 20 MiB per image, 4 images per request. Enforced by a streaming `LimitedReader`; do NOT trust `Content-Length`. Connection severed if cap exceeded mid-stream.
> - **Content-Type:** must sniff to `image/png|jpeg|gif|webp` (first 512 bytes); the `Content-Type` header is verified to match the sniffed type — mismatch rejects (defeats MIME spoofing).
> - **Decompression guard:** if response uses `Content-Encoding: gzip|deflate|br`, decompression is bounded by the same 20 MiB cap on DECOMPRESSED bytes; ratio > 10× triggers `DECOMPRESSION_BOMB` error.
> - **Timeouts:** 5 s DNS, 5 s TCP+TLS handshake, 15 s total wall-clock per fetch.
> - **Per-tenant fetch budget:** 100 multimodal fetches/min per tenant via Valkey sliding window `mmfetch:{tenant_id}:{minute}`. 429 on exceed; separate from chat rate limit. Fail-open on Valkey outage with `llms_mmfetch_ratelimit_skipped_total` counter.
> - **Egress NetworkPolicy defence-in-depth (deploy-target / K8s form only):** dedicated egress policy on the gateway pod that drops at the CNI layer any traffic to RFC1918/link-local destinations from the fetcher's namespace. Application-layer bypass alone is not enough to reach internal services. No compose equivalent — in the compose runtime the fetcher stays disabled (pass-through default), so this control is moot until the infra phase.
> - **Audit log:** every fetch emits `cypherx.llms.multimodal_fetch` event (host + final-status only — NO path, NO query string, NO response body) for security review.

**Schema rationale:** We adopt the OpenAI-style shape because it has the broadest ecosystem support (OpenAI, Mistral, Groq, Gemini all accept it; Anthropic is the main one that doesn't). The gateway's Anthropic adaptor translates:
- `messages[].role = "system"` → Anthropic top-level `system` field (Anthropic has no system message role; concatenate multiple system messages with `\n\n`)
- `messages[].role = "tool"` → Anthropic `tool_result` content block
- `tool_calls` in assistant message → Anthropic `tool_use` content block
- `tools[].function` → Anthropic `tools[]` with `input_schema`
- `tool_choice` → Anthropic `tool_choice` (any/auto/tool/none)
- Multimodal `image_url` → Anthropic `image` content block (URL-source pass-through by default; base64 inlining only when the config-gated fetcher is enabled — see Amendment Log)
- `response_format`:
    - `{ "type": "text" }` (or absent) → no-op, pass through.
    - `{ "type": "json_object" }` or `{ "type": "json_schema", ... }` → **reject with 422
      `MODEL_UNSUPPORTED`**, error body listing which models support structured output
      (OpenAI gpt-4o family). First-cycle decision: NO silent prompt-engineering fallback —
      that produces best-effort JSON that's a debugging trap. Structured output on Anthropic
      via tool-wrapper translation is tracked as 📋 (Full Enterprise checklist).
- **Sampling-param range mismatches (provider ranges differ — failing to clamp surfaces as cryptic 400s deep in the adaptor):**
    - `temperature`: unified range `[0, 2]` (OpenAI). Anthropic adaptor clamps to `[0, 1]` and emits `X-Cypherx-Param-Clamped: temperature` response header so the caller knows the value was adjusted. No silent rounding without telemetry.
    - `top_p`: unified `[0, 1]`. Pass-through to both providers (compatible).
    - `stop`: OpenAI accepts ≤ 4 strings; Anthropic accepts up to 4 in `stop_sequences`. Unified cap = 4. Validation 400 if > 4.
- **`max_tokens` per-model ceiling (de-hardcoded — see Amendment Log):** Each model has a hard provider-side cap (seed examples: Claude Sonnet 4.6 = 8192, Claude Opus 4.8 = 32000, gpt-4o = 16384, gpt-4o-mini = 16384). Gateway holds these in `llms.model_capabilities` (DDL in Component 2 — pulled into Phase 3; was deferred to Phase 13) and rejects with 400 `MAX_TOKENS_EXCEEDED` before dispatch. The DB is the single authority: capabilities load at startup (startup-blocking — the gateway refuses to serve if the load fails) and refresh every 60 s. The in-code caps map is a documented test fixture only — never consulted at runtime.
- **Tool-name regex:** OpenAI and Anthropic both require `^[a-zA-Z0-9_-]{1,64}$`. Gateway validates at the unified schema layer — reject 400 `INVALID_TOOL_NAME` before adaptor dispatch (avoids "weird gateway 500" from provider-side rejection).
- **Parallel tool calls:** OpenAI emits multiple `tool_calls[]` per turn by default; Anthropic emits parallel `tool_use` blocks unless `disable_parallel_tool_use: true`. Unified flag `parallel_tool_calls: true` (default true) → Anthropic adaptor sets `disable_parallel_tool_use: !parallel_tool_calls`. xAgent's execution loop MUST handle ≥ 1 tool call per turn.
- **Refusal / safety stop reasons:** Anthropic returns `stop_reason: end_turn | stop_sequence | tool_use | max_tokens | refusal`; OpenAI returns `finish_reason: stop | length | tool_calls | content_filter`. Mapping the adaptor applies:
    - Anthropic `end_turn` / `stop_sequence` → unified `stop`
    - Anthropic `tool_use` → unified `tool_calls`
    - Anthropic `max_tokens` → unified `length`
    - Anthropic `refusal` (Claude 4.x family) → unified `content_filter`. xAgent treats this as terminal — do not loop.

**Unified Response (non-streaming):**
```json
{
  "id":      "<response-uuid>",
  "model":   "claude-sonnet-4-6",
  "object":  "chat.completion",
  "created": 1716384000,
  "choices": [
    {
      "index":   0,
      "message": {
        "role": "assistant",
        "content": "...",                   // text (may be null if only tool_calls)
        "tool_calls": [                     // present when model wants to call a tool
          { "id": "call_2", "type": "function",
            "function": { "name": "web_search", "arguments": "{\"query\":\"latest news\"}" } }
        ]
      },
      "finish_reason": "stop"               // stop | length | tool_calls | content_filter | budget_exceeded
    }
  ],
  "usage": {
    "prompt_tokens":         1200,
    "completion_tokens":     450,
    "total_tokens":          1650,
    "cached_prompt_tokens":  0,        // tokens served from provider prompt cache (cheap)
    "cache_creation_tokens": 0,        // tokens written to provider prompt cache (one-time premium)
    "cost_usd":              0.00823
  }
}
```

> **Mandatory normalization:** Anthropic returns `stop_reason: "tool_use"` and `tool_use` content blocks. The Anthropic adaptor MUST convert these to `finish_reason: "tool_calls"` and `tool_calls[]` so xAgent's execution loop is provider-agnostic. Same for OpenAI → unified pass-through.

> **Cache token normalization:**
> - Anthropic: `cached_prompt_tokens = usage.cache_read_input_tokens`, `cache_creation_tokens = usage.cache_creation_input_tokens`.
> - OpenAI: both fields = 0 (no public cache-token reporting yet).
> Without these fields, billing reconciliation cannot break out cache savings — a real revenue/reporting gap.

**Cost calculation (explicit formula):**
```
cost_usd = (prompt_tokens          / 1000) * input_cost_per_1k_tokens
        +  (completion_tokens      / 1000) * output_cost_per_1k_tokens
        +  (cached_prompt_tokens   / 1000) * cached_input_cost_per_1k_tokens     // discounted
        +  (cache_creation_tokens  / 1000) * cache_creation_cost_per_1k_tokens   // premium

Notes:
  - For Anthropic, prompt_tokens reported by the provider EXCLUDES cached_prompt_tokens
    and cache_creation_tokens. Cost is the sum of the three line items. Do NOT double-count.
  - Rates: llms.provider_pricing table (schema below). CI fails if rates older than 90 days.
  - A weekly cron Job emits a Slack alert if any row's updated_at < NOW() - 60 days.
```

**Cost-rate table (referenced above, missing from prior draft):**
```sql
CREATE TABLE llms.provider_pricing (
  provider                          VARCHAR(50)   NOT NULL,
  model                             VARCHAR(100)  NOT NULL,
  input_cost_per_1k_tokens          NUMERIC(12,8) NOT NULL,
  output_cost_per_1k_tokens         NUMERIC(12,8) NOT NULL,
  cached_input_cost_per_1k_tokens   NUMERIC(12,8) NOT NULL DEFAULT 0,
  cache_creation_cost_per_1k_tokens NUMERIC(12,8) NOT NULL DEFAULT 0,
  currency                          CHAR(3)       NOT NULL DEFAULT 'USD',
  effective_from                    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  updated_at                        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  PRIMARY KEY (provider, model, effective_from)
);
-- Seeded by migration with current Anthropic + OpenAI rates.
-- Updates: PR-only (Platform Ops); CI lints schema; weekly cron alerts on staleness.
-- Platform-scoped table (no tenant_id, no RLS).
```

**Unified Embeddings Request:**
```json
POST /v1/embeddings

{
  "model":  "text-embedding-3-small",        // alias "embed" maps here by default
  "input":  ["text to embed", "second text"], // string OR array of strings (batched, max 256 items)
  "dimensions": 1536,                         // optional; only if model supports truncation
  "metadata": { "user_label": "rag-ingest-batch-42" }   // free-form tags only; no identity fields
}

Response:
{
  "data": [ { "index": 0, "embedding": [0.012, ...] }, ... ],
  "model": "text-embedding-3-small",
  "usage": { "prompt_tokens": 14, "cost_usd": 0.0000028 },
  "dimensions": 1536
}
```

> **Embeddings batch cap:** `input` array MUST contain ≤ 256 items. Over-limit requests return `VALIDATION_ERROR`. Declared in OpenAPI.

> **Scope:** `POST /v1/embeddings` requires JWT scope `llm:invoke` — the same scope as chat completions. No separate `embed:generate` scope; one grant covers both. Phase 2 default policy already permits `llm:invoke`.

---

### Component 2 — Model Alias Registry ⚡

**What it is:** A mapping from friendly alias names to real model IDs, configurable per tenant.

**PostgreSQL (`llms.model_aliases`):**
```sql
CREATE TABLE llms.model_aliases (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID,               -- NULL = platform default
  alias        VARCHAR(50) NOT NULL,   -- e.g., "fast", "smart", "vision", "code"
  model_id     VARCHAR(100) NOT NULL,  -- e.g., "claude-haiku-4-5-20251001"
  provider     VARCHAR(50) NOT NULL,   -- anthropic | openai | google | ...
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (tenant_id, alias)
);

-- Platform defaults (seeded on startup):
-- alias: "fast"    → claude-haiku-4-5-20251001  (anthropic)
-- alias: "smart"   → claude-sonnet-4-6           (anthropic)
-- alias: "code"    → claude-sonnet-4-6           (anthropic)
-- alias: "vision"  → claude-sonnet-4-6           (anthropic)
-- alias: "embed"   → text-embedding-3-small      (openai)
```

**Resolution order:** tenant-specific alias → platform default alias → literal model ID (if no alias match).

**Model capability registry (`llms.model_capabilities`) — pulled into Phase 3 (de-hardcoding amendment; was Phase 13):**
```sql
CREATE TABLE llms.model_capabilities (
  provider       VARCHAR(50)  NOT NULL,
  model          VARCHAR(100) NOT NULL,
  max_tokens     INTEGER      NOT NULL,            -- hard provider-side completion cap
  modalities     TEXT[]       NOT NULL DEFAULT '{text}',  -- text | image | embedding
  embedding_dim  INTEGER,                          -- non-NULL for embedding models; surfaced via GET /v1/models (WP06)
  updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (provider, model)
);
-- Platform-scoped table (no tenant_id, no RLS). Seeded by migration; updates PR-only.
```

> **DB is the single authority (de-hardcoded — see Amendment Log):** aliases, pricing, and model capabilities are loaded from Postgres at startup (**startup-blocking** — the gateway exits non-zero if the load fails; no silent fallback to stale data) and refreshed every **60 s** thereafter. Any in-code alias/cap/pricing map exists only as a documented **test fixture** and is never consulted at runtime.
>
> **Seed reconciliation (audit-found drift):** the seed migration — not just this doc — MUST carry the `embed`/`code`/`vision` aliases listed above; the opus model id is reconciled to the provider's current catalog (`claude-opus-4-7` → `claude-opus-4-8` drift found in audit — verify against the live catalog at seed-write time); embedding-model pricing rows are seeded in `llms.provider_pricing` (WP06).

---

### Component 3 — Provider Adaptor Layer ⚡

**Interface (every provider must implement):**
```
IProviderAdaptor
├── Chat(request UnifiedChatRequest)    → (UnifiedChatResponse, error)
├── ChatStream(request)                 → (Stream<UnifiedChunk>, error)
├── Embed(request UnifiedEmbedRequest)  → (UnifiedEmbedResponse, error)
├── ListModels()                        → ([]ModelInfo, error)
└── Health()                            → (ProviderHealth, error)
```

**First cycle providers to implement:**

| Provider | Models supported | Notes |
|----------|-----------------|-------|
| Anthropic | claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5-20251001 | Messages API (opus id reconciled per Amendment Log) |
| OpenAI | gpt-4o, gpt-4o-mini, text-embedding-3-small | Chat completions API |

**Provider config (PostgreSQL — `llms.providers`):**
```sql
CREATE TABLE llms.providers (
  provider_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID,                -- NULL = platform-managed key
  provider     VARCHAR(50) NOT NULL,
  key_ref      VARCHAR(512) NOT NULL,   -- '<backend>:<backend-specific-ref>', see registry below
  status       VARCHAR(20)  NOT NULL DEFAULT 'active',
  is_byok      BOOLEAN      NOT NULL DEFAULT false,
  priority     INTEGER      NOT NULL DEFAULT 100,  -- lower = higher priority
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- (Amended) The literal key_ref CHECK constraint is REPLACED by a registry table —
-- backend validity is data, not schema:
CREATE TABLE llms.secret_backends (
  backend      VARCHAR(50) PRIMARY KEY,   -- 'sealed' | 'env' | 'secretsmanager' (future)
  enabled      BOOLEAN     NOT NULL DEFAULT true,
  description  TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Seed: ('sealed', true), ('env', true), ('secretsmanager', false — optional future/cloud backend).
-- Platform-scoped table (no tenant_id, no RLS). App-layer validation: every key_ref's
-- '<backend>' prefix must exist AND be enabled in llms.secret_backends at insert time.
```

> **`key_ref` format (registry-driven — replaces the prior `doppler:`/`secretsmanager:` literal CHECK, see Amendment Log):**
> `key_ref` = `<backend>:<backend-specific-ref>`. The gateway has a single key-resolver
> module that dispatches on the `<backend>` prefix; adding a new backend (e.g., Vault) is
> one resolver module + one `llms.secret_backends` row — no schema change, no constraint update.
>
> **First-cycle backends and resolver semantics:**
> - `env:<VAR>` — platform-managed keys, e.g. `env:ANTHROPIC_API_KEY`. The resolver simply
>   reads `os.Getenv(<VAR>)`; the secret is pre-injected at deploy time (compose `.env`
>   first cycle; Doppler-operator injection in the cloud form — either way there is NO
>   runtime secret-store API call). An `env:` ref whose variable is unset at boot is a
>   fatal startup error — log the missing var and exit non-zero.
> - `sealed:v1:<uuid>` — BYOK keys. The raw key is AES-256-GCM **envelope-encrypted in
>   Postgres** (`llms.sealed_secrets`, DDL in Component 8) under an env-supplied KEK
>   (`LLMS_BYOK_KEK`) — the same pattern Auth already uses for signing keys. `<uuid>` is
>   the `sealed_secrets` row id. The resolver decrypts in-process and caches the plaintext
>   for 15 minutes; cold decrypt on cache miss is synchronous (acceptable — BYOK paths are
>   not the first-cycle hot path).
> - `secretsmanager:arn:aws:...` — **optional FUTURE backend (cloud form)**, disabled in the
>   registry until the infra phase provisions AWS. When enabled: runtime fetch via AWS SDK +
>   IRSA role scoped to `cypherx/byok/*`, 15-min in-process cache, cache invalidated on ARN
>   suffix change (rotation).
> Do NOT add code that fetches from any secret store at runtime for `env:` refs — env
> injection at deploy time is the contract; a runtime fetch path would re-introduce a
> long-lived store credential in every pod/container.

> **Mixed-scope RLS (this table holds platform AND tenant rows):**
> ```sql
> CREATE POLICY providers_read ON llms.providers FOR SELECT
>   USING (tenant_id = current_setting('app.tenant_id')::uuid OR tenant_id IS NULL);
> CREATE POLICY providers_write ON llms.providers FOR ALL
>   USING      (tenant_id = current_setting('app.tenant_id')::uuid)
>   WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
> ```
> Same pattern applies to `llms.model_aliases` and `llms.budgets`. Tenants can READ
> platform defaults but cannot mutate them; mutation of platform rows requires
> `platform:admin` scope via a separate connection role.

---

### Component 4 — Token Counting & Usage Tracking ⚡

**What it is:** Every LLM call records token usage and cost. This feeds billing and budget enforcement.

**PostgreSQL (`llms.usage_records`):**
```sql
CREATE TABLE llms.usage_records (
  id                     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  llm_call_id            UUID         NOT NULL,            -- gateway-minted per provider call; THE billing uniqueness key (see provenance note)
  request_id             UUID         NOT NULL,            -- = X-Request-ID header; NON-UNIQUE correlation column (see provenance note below)
  tenant_id              UUID         NOT NULL,
  agent_id               UUID,                              -- nullable: non-agent caller (external dev via api_key)
  api_key_id             UUID,                              -- per-key attribution for billing dashboards (Contract 18)
  principal_type         VARCHAR(20)  NOT NULL,             -- 'agent' | 'api_key' | 'service' | 'on_behalf_of_user'
  task_id                UUID,                              -- nullable: embeddings / non-task calls
  trace_id               UUID         NOT NULL,            -- Contract 8 mandates traceparent on every request
  provider               VARCHAR(50)  NOT NULL,
  model                  VARCHAR(100) NOT NULL,
  prompt_tokens          INTEGER      NOT NULL,
  completion_tokens      INTEGER      NOT NULL,
  total_tokens           INTEGER      NOT NULL,
  cached_prompt_tokens   INTEGER      NOT NULL DEFAULT 0,
  cache_creation_tokens  INTEGER      NOT NULL DEFAULT 0,
  cost_usd               NUMERIC(12,8) NOT NULL,
  duration_ms            INTEGER,
  status                 VARCHAR(20),   -- success | error | timeout
  created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- (Amended) Billing uniqueness key — gateway-minted, one per provider call:
ALTER TABLE llms.usage_records ADD CONSTRAINT uq_usage_llm_call UNIQUE (tenant_id, llm_call_id);
-- request_id is deliberately NOT unique: one upstream X-Request-ID legitimately spans
-- multiple LLM calls (xAgent forwards it across loop iterations — Contract 8).
CREATE INDEX idx_usage_request_id  ON llms.usage_records(request_id);

CREATE INDEX idx_usage_tenant_id   ON llms.usage_records(tenant_id, created_at DESC);
CREATE INDEX idx_usage_agent_id    ON llms.usage_records(agent_id, created_at DESC) WHERE agent_id IS NOT NULL;
CREATE INDEX idx_usage_api_key_id  ON llms.usage_records(api_key_id, created_at DESC) WHERE api_key_id IS NOT NULL;

-- RLS (tenant-scoped table — Contract 13):
ALTER TABLE llms.usage_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY usage_tenant_isolation ON llms.usage_records FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> **`api_key_id` rationale.** External developers expect per-key usage breakdowns (OpenAI's project keys, Anthropic's API keys). Without this column, `GET /v1/usage` cannot attribute spend to specific keys — a basic SaaS requirement. `principal_type` lets queries discriminate "what agent burned tokens" from "what external API key burned tokens" cleanly.

> Note: PK switched from `BIGSERIAL` to `UUID`. A global serial leaks cross-tenant
> write-volume via the sequence value (side channel) and fights the natural
> `(tenant_id, created_at)` query pattern.

> Note: renamed `latency_ms` → `duration_ms` to match Contract 5 first-cycle event
> field names. The Kafka event payload uses the same name; no translation needed.

> **`llm_call_id`, `request_id`, and `trace_id` provenance (MANDATORY — single source of truth):**
> - `llm_call_id` (amended — see Amendment Log) is **minted by the gateway**, one fresh UUIDv4
>   per provider call. It is THE uniqueness key for billing (`UNIQUE (tenant_id, llm_call_id)`)
>   precisely because `request_id` is caller-controlled and legitimately repeats: Contract 8
>   makes callers forward `X-Request-ID`, so two completions inside one upstream request share
>   a `request_id` — both MUST bill. Carried in the Kafka payload alongside `request_id`.
>   Callers change nothing; the field never appears in the request schema.
> - `request_id` = value of the inbound `X-Request-ID` header. The ingress `correlation-id`
>   mechanism (Kong plugin in the cloud form — Phase 1 Component 8; in the compose runtime the
>   caller supplies it) injects this on every external request and the gateway forwards it
>   on every internal call (Contract 8). The gateway MUST NOT mint its own request_id when
>   the header is present. If the header is absent (internal-only call path that bypassed
>   the ingress), the gateway generates a UUIDv4, sets `X-Request-ID` on outbound calls, and emits
>   a WARN log `request_id_generated_fallback=true` so the gap can be tracked.
>   It is a **non-unique correlation column** — never a uniqueness/idempotency key for billing.
> - `trace_id` = the 16-byte trace ID parsed from `traceparent` (Contract 8). Stored as UUID
>   (same 128-bit width). The gateway MUST NOT proceed without a trace ID — if `traceparent`
>   is missing, synthesize one, log WARN, and start a new trace rather than write NULL.
> - Neither field is ever taken from the request body. Gateway rejects with 400 if a body
>   field named `request_id` or `trace_id` appears anywhere outside `metadata` (which itself
>   rejects identity keys — see Component 1).

**Outbox pattern (REQUIRED — guarantees DB write and Kafka event don't diverge):**

```sql
CREATE TABLE llms.outbox (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,        -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,        -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX idx_outbox_unpublished ON llms.outbox (created_at) WHERE published_at IS NULL;
```

Write path (one transaction):
```
BEGIN;
  INSERT INTO llms.usage_records (...) VALUES (...);   -- NO ON CONFLICT on the hot path (amended):
                                                       -- a duplicate llm_call_id here is a BUG —
                                                       -- fail loudly (unique violation → 500 + alert),
                                                       -- never silently drop a billing row.
  INSERT INTO llms.outbox (topic, partition_key, payload) VALUES
    ('cypherx.llms.request.completed', tenant_id::text, <Contract 5 envelope JSON>);
COMMIT;
```
Only the billing-replay worker (below) may use `ON CONFLICT (tenant_id, llm_call_id) DO NOTHING` —
replay is the one legitimate duplicate case.

Publisher loop (separate goroutine/worker, one per pod):
```
SELECT ... FROM llms.outbox WHERE published_at IS NULL ORDER BY created_at LIMIT 100
  → publish to Kafka with partition_key
  → UPDATE published_at = NOW() on success
  → UPDATE attempts++, last_error = ... on failure (with exponential backoff)
  → DLQ after 10 attempts (publish to cypherx.llms.request.completed.dlq with reason)
```

A nightly job deletes outbox rows where `published_at < NOW() - INTERVAL '7 days'`.

**Synchronous billing-write failure mode (provider already returned tokens — those are paid for):**

The `(usage_records + outbox)` transaction runs AFTER the provider returns and BEFORE we respond to the client. If Postgres is unreachable or the transaction rolls back at this point, the tenant has been charged by the provider but we have no durable record. **Do NOT 5xx the client** — they already paid; refusing the response just costs them tokens and looks like a gateway bug.

```
1. Write the usage envelope to a local-disk journal:
     /var/lib/llms-gateway/billing-replay/{llm_call_id}.json
     (atomic write: O_TMPFILE + linkat; durable-volume-backed so the journal survives
     restart — compose named volume first cycle; PVC in the cloud form).
2. Emit `llms_billing_write_failed_total{reason="db_unreachable"}`.
     Pages oncall at rate >= 1/min sustained 2 min.
3. Serve the response with header `X-Cypherx-Billing-Pending: true`.
4. Background replay worker (startup + every 30 s) drains the journal into Postgres
   keyed on `llm_call_id` — re-running the original INSERT is safe because
   `(tenant_id, llm_call_id)` is unique on `usage_records`, and the replay worker is the
   ONLY writer permitted to use `ON CONFLICT (tenant_id, llm_call_id) DO NOTHING`
   (amended — the hot path never uses ON CONFLICT; see write-path note above).
5. Journal backpressure: hard cap 10 000 entries on disk. When the journal exceeds
   1 000 entries, /readyz starts failing so traffic shifts to healthier pods. At
   10 000, the gateway WILL 5xx new requests with `BILLING_DEGRADED` — better to
   refuse than to silently lose accounting.
```

A second failure mode: idempotency Valkey write fails after the DB commit succeeded. The DB is authoritative for billing — log WARN; a client retry will replay the provider call (extra cost), but billing remains correct. Acceptable trade-off given Valkey's fail-open posture across the rest of the system.

**Idempotency (Contract 9 — MANDATORY for `POST /v1/chat/completions` and `POST /v1/embeddings`):**

LLM calls cost real money. An accidental client retry (network blip, retry-on-timeout in an
HTTP client, agent loop) must not double-bill the tenant. Per Contract 9:

```
Header:  Idempotency-Key: <client-generated-uuid>     (RECOMMENDED for chat, MAY for embeddings)
Storage: Valkey
Key:     llm-idemp:{tenant_id}:{agent_id}:{route}:{idempotency_key}
TTL:     24h
Value:   { "status": "in_flight" | "completed",
           "response_id":  "<usage_records.id>",
           "http_status":  200,
           "body_sha256":  "<hex>",
           "body_compressed": "<gzip+base64 of unified response, ≤32 KiB>" }
```

Flow:
1. On request arrival, compute the Valkey key. `SET ... NX EX 86400` with `status=in_flight`.
2. If SET succeeds → proceed. After the provider call returns and the (usage_records, outbox)
   transaction commits, `SET` the same key to `status=completed` with the response cached.
3. If SET fails (key exists):
    - `status=completed` → return the cached body verbatim with `Idempotent-Replayed: true` header.
      Do **not** write a usage_record, do **not** publish a Kafka event (already done on the
      first hit).
    - `status=in_flight` → return 409 `IDEMPOTENCY_REQUEST_IN_FLIGHT` with `Retry-After: 2`.
      Client retries; second response will be `completed`.
      (Error-code spelling amended to the Contract 2 `IDEMPOTENCY_*` family — see Amendment Log.)
4. If response body > 32 KiB (rare — long completions), cache `body_sha256` only; on replay,
   re-fetch by `response_id` from `usage_records` + provider-response archive (📋). First
   cycle: cap at 32 KiB and document the limit; over-cap responses get `Idempotent-Replayed`
   skipped on retry and re-execute (acceptable for first cycle — chat completions are
   typically well under 32 KiB).

**Streaming (`stream=true`) — DECIDED semantics (amended; resolves the ⚡ streaming × ⚡ idempotency
collision):** the Idempotency-Key is **recorded but replay-exempt**. The gateway writes the Valkey
key (24h TTL, `status` only — no body caching for streams) so duplicate submissions are *detectable*
(telemetry counter `llms_idempotency_stream_duplicate_total`), but the lookup does NOT short-circuit:
a duplicate stream request re-executes the provider call and every streamed response that carried an
Idempotency-Key includes the header `Idempotent-Replayed: false`. Rationale: byte-accurate SSE replay
from cache is not first-cycle scope; recording the key preserves duplicate-detection and reserves the
upgrade path to true stream replay (📋).

Valkey-outage behavior: same FAIL OPEN rule as Component 5 rate limiter — skip idempotency
check, log WARN, emit `llms_idempotency_skipped_total{reason="valkey_unavailable"}`. The
window during outage is small and the alternative (rejecting all paid calls) is worse.

**Kafka event published after every successful LLM call (matches Contract 5 first-cycle spec):**
```json
Topic: cypherx.llms.request.completed
Partition key: tenant_id
Payload (inside Contract 5 envelope's "payload" field):
{
  "llm_call_id":          "<uuid>",   // amended: gateway-minted billing key — payload carries BOTH ids
  "request_id":           "<uuid>",   // non-unique correlation id (Contract 8)
  "tenant_id":            "<uuid>",
  "agent_id":             "<uuid>",
  "task_id":              "<uuid>",
  "provider":             "anthropic",
  "model":                "claude-sonnet-4-6",
  "prompt_tokens":        1200,
  "completion_tokens":    450,
  "cached_prompt_tokens": 0,
  "cache_creation_tokens": 0,
  "cost_usd":             0.00823,
  "duration_ms":          342,
  "status":               "success",
  "trace_id":             "<uuid>"
}
```

---

### Component 5 — Basic Rate Limiting ⚡

**What it is:** Request + token rate limiting to prevent any one agent or tenant from consuming all LLM capacity.

**Strategy (using Valkey) — DECIDED semantics (amended; a token PRE-check is impossible because completion size is unknowable before dispatch):**
```
Key: ratelimit:llms:req:{tenant_id}:{window}   → request count   (PRE-flight check)
Key: ratelimit:llms:tok:{tenant_id}:{window}   → token debit     (POST-hoc, from actual usage)
Window: 60s (per-minute limits)

Algorithm: fixed window (simpler, first cycle); sliding window log is a 📋 refinement

1. requests_per_min — enforced BEFORE dispatch: increment-and-check the request counter;
   429 RATE_LIMIT_EXCEEDED on exceed. This is the only pre-flight gate.
2. tokens_per_min — enforced POST-hoc: after the provider returns, debit total_tokens
   (from ACTUAL usage) against the current window. If the window is already over-spent
   when a request arrives, reject 429 before dispatch. A single request may overshoot
   the token cap by its own size — accepted first-cycle behavior.
3. Predictive token limiting (estimate-before-dispatch) is 📋 Full Enterprise.

Limits (configurable per tenant plan — stored in llms.rate_limits table):
  free:       10,000 tokens/min per tenant
  pro:       100,000 tokens/min per tenant
  enterprise: unlimited (soft cap only, alerts)

On limit exceeded → return 429 with error code: RATE_LIMIT_EXCEEDED
  Include: Retry-After header, remaining tokens, window reset time
```

**Rate-limit plan table (DDL — referenced above, was missing):**
```sql
CREATE TABLE llms.rate_limits (
  plan              VARCHAR(20)  PRIMARY KEY,           -- 'free' | 'pro' | 'enterprise'
  tokens_per_min    INTEGER      NOT NULL,              -- 0 = unlimited (alert-only)
  requests_per_min  INTEGER      NOT NULL,              -- request-count ceiling (defence-in-depth)
  burst_multiplier  NUMERIC(4,2) NOT NULL DEFAULT 1.50, -- short-burst headroom over the steady cap
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Per-tenant plan assignment is INTERIM-OWNED BY AUTH (amended — see Amendment Log; the
-- previous owner, px0/platform-mgmt, is deferred and nothing carried a plan field):
-- `auth.tenants.plan` (default 'free'), surfaced BOTH as a `plan` claim on Auth-minted JWTs
-- AND via Auth's limits endpoint. The gateway reads the claim (limits endpoint as fallback),
-- caches it 60s in Valkey under key `tenant-plan:{tenant_id}`, and joins
-- `plan = rate_limits.plan` at request time. NULL/missing plan → treat as 'free'.
-- Ownership migrates to platform-mgmt (px0) in the enterprise wave; the gateway contract
-- (claim + 60s cached lookup) is unchanged by that migration.

-- Platform-scoped table (no tenant_id, no RLS). Mutations require platform:admin scope.

-- Seed (Atlas migration):
INSERT INTO llms.rate_limits (plan, tokens_per_min, requests_per_min) VALUES
  ('free',        10000,   60),
  ('pro',        100000,  600),
  ('enterprise',      0, 6000);     -- 0 tokens_per_min = unlimited (alert-only)
```

> Per-agent ceilings are 📋 (Full Enterprise checklist). First cycle enforces per-tenant only.

> **Valkey outage behavior — FAIL OPEN with telemetry:**
> If Valkey is unreachable or returns errors:
> - Skip rate-limit enforcement for the request.
> - Emit Prometheus counter `llms_ratelimit_skipped_total{reason="valkey_unavailable"}`.
> - Log WARN per request: `"rate limit skipped: <error>"`.
> - Alertmanager fires when rate ≥ 10/min for ≥ 2 min.
> - `/readyz` does NOT fail (rate limiter is a soft guard, not a hard control).
>
> Rationale: the budget hard-stop (Component 10) is the real cost ceiling. Rate
> limiting smooths bursts and protects providers from one runaway tenant —
> losing it briefly is a degradation, not an outage. Failing closed would take
> the whole platform down whenever Valkey hiccups.

---

### Component 6 — Streaming Support ⚡

**What it is:** Server-Sent Events (SSE) streaming for real-time token delivery.

**Request:**
```json
{ ..., "stream": true }
```

**Response (SSE format):**
```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive

data: {"id":"resp-1","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hello"},"index":0}]}

data: {"id":"resp-1","object":"chat.completion.chunk","choices":[{"delta":{"content":" world"},"index":0}]}

data: [DONE]
```

**Implementation notes:**
- Backpressure: if client disconnects, cancel the provider API request (use `context.Cancel` / `AbortController`).
- Timeout: 120s max for a streaming response (matches Contract 9 streaming override).
- **Idempotency (amended — decided semantics, see Component 4):** streams are **replay-exempt**. An `Idempotency-Key` on a `stream=true` request is recorded (duplicate-detection telemetry) but never short-circuits; every streamed response that carried the header includes `Idempotent-Replayed: false`.
- The gateway MUST override the client-supplied `stream_options` on OpenAI streaming calls to force `include_usage: true`. Client-supplied `stream_options` are silently merged (server wins on `include_usage`).
- **Per-provider token-usage in streams (must be normalized):**
  - **Anthropic** — emits `message_start` event with `usage.input_tokens` and `message_delta` events with running `usage.output_tokens`; final `message_stop` carries the authoritative totals. Gateway emits a final unified `data: { "usage": {...} }` event.
  - **OpenAI** — only emits usage if the request includes `stream_options: { include_usage: true }`; the gateway MUST set this flag on every OpenAI streaming call so usage arrives in the last data event.
  - **Gemini / others** — fall back to a tokenizer (tiktoken for OpenAI-compatible, claude_tokenizer for Anthropic) on the assembled text if the provider does not report usage.

- **Tool-call streaming (gated on `stream_options.aggregate_tool_calls`, default `true` — reserve the wire-format switch NOW so future delta-mode is a flag flip, not a breaking change):**
  Providers emit `tool_calls` as deltas (argument string grows char-by-char).
  - **`aggregate_tool_calls: true` (first-cycle default):** Gateway assembles deltas into a complete `tool_calls[]` object and emits one final SSE event:
    ```
    data: { "type": "tool_calls", "tool_calls": [...] }
    ```
    xAgent (first-cycle client) waits for tool-call completion before acting, so partial deltas have no value. Simplifies the wire protocol and avoids partial-JSON parsing in every client.
  - **`aggregate_tool_calls: false` (📋 Phase 12 — frontend pass-through):** Gateway forwards OpenAI-shape delta events verbatim and translates Anthropic `input_json_delta` events into the same delta shape so the wire format is provider-agnostic. Reserved at the schema level on day one so future interactive UIs (typing animations on tool args) opt in via a flag flip, not a wire-protocol break.
  - **First-cycle enforcement:** the flag is accepted in the request schema; `false` is rejected with 422 `FEATURE_NOT_YET_IMPLEMENTED` until Phase 12 lands. Reserving the flag without enabling it prevents silent semantic drift (e.g., a client setting `false` and assuming deltas while the server is still aggregating).

- **Mid-stream error handling (REQUIRED):**
  If the provider stream errors mid-flight (provider 5xx, network drop, internal error,
  context cancelled), the gateway:
  1. Emits an SSE error event:
     ```
     event: error
     data: { "error": { "code": "<error-code>", "message": "<safe-message>" } }
     ```
  2. Closes the stream (no `[DONE]` after `event: error`).
  3. Writes a `usage_records` row with `status = error`, `completion_tokens = tokens_streamed_so_far`, partial `cost_usd` calculated from streamed token count.
  4. Publishes the standard `cypherx.llms.request.completed` event via the outbox (with `status = error`) — billing still captures partial cost.

- **Last SSE event before `[DONE]`** must always be:
  ```
  data: { "id": "...", "object": "chat.completion.chunk",
          "choices": [{ "delta": {}, "finish_reason": "stop|tool_calls|length", "index": 0 }],
          "usage": { "prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...,
                     "cached_prompt_tokens": ..., "cache_creation_tokens": ..., "cost_usd": ... } }
  ```

---

### Component 7 — Routing & Fallback 📋

**What it is:** Intelligent routing — pick the best provider/model based on requirements; auto-failover if primary is down.

**Routing config per request (optional fields in request body):**
```json
{
  "routing_hints": {
    "prefer": "cheapest | fastest | most_capable",
    "exclude_providers": ["openai"],
    "require_streaming": true
  }
}
```

**Fallback chain:**
```
Primary provider fails (5xx, timeout, rate limit)
  → Wait 0ms → Try secondary provider from agent's fallback list
  → If secondary fails → Return 503 SERVICE_UNAVAILABLE
  → Log: cypherx.llms.provider.failover event to Kafka
```

---

### Component 8 — BYOK (Bring Your Own Key) ⚡ (PROMOTED for external SaaS)

**Promoted to first-cycle** because external customers expect to bring their own OpenAI/Anthropic keys from day one — both for cost attribution (their bill, not ours) and for compliance (their key, their data plane). Without BYOK at ⚡, any external customer launch is blocked.

**What it is:** Tenants register their own API keys. The gateway proxies through their key instead of platform-managed keys.

```
POST /v1/keys
Body: {
  "provider": "openai",
  "api_key": "sk-...",
  "name": "my-openai-key",
  "models": ["gpt-4o", "gpt-4o-mini"]
}

Storage (AMENDED — pluggable secret backend, see Amendment Log; the previous
AWS-Secrets-Manager-only design was unimplementable in the compose/Neon runtime):

  - FIRST CYCLE (`sealed:v1` backend): the raw key is AES-256-GCM envelope-encrypted
    and stored in Postgres — a per-secret random DEK encrypts the key; the DEK is
    wrapped by the env-supplied KEK `LLMS_BYOK_KEK`. This is the SAME envelope pattern
    Auth already uses for its signing keys.

      CREATE TABLE llms.sealed_secrets (
        id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id    UUID        NOT NULL,
        ciphertext   BYTEA       NOT NULL,   -- AES-256-GCM: nonce || ct || tag
        wrapped_dek  BYTEA       NOT NULL,   -- DEK wrapped under LLMS_BYOK_KEK
        kek_version  INTEGER     NOT NULL DEFAULT 1,
        status       VARCHAR(20) NOT NULL DEFAULT 'current',  -- current | pending-deletion
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
      );
      -- RLS: standard tenant-scoped policy (Contract 13).

    Reference stored in llms.providers.key_ref as `sealed:v1:<sealed_secrets.id>`.
    The raw key is NEVER logged, never returned by any API after registration, and
    the plaintext exists only in the resolver's 15-min in-process cache.
  - Platform-managed keys use `env:<VAR>` refs (env-injected at deploy time — never
    tenant secrets; see Component 3 resolver semantics).
  - FUTURE (cloud form, optional): `secretsmanager:` backend — raw key in AWS Secrets
    Manager at `cypherx/byok/<tenant_id>/<provider>-<suffix>` with KMS encryption via
    the per-tenant CMK alias `alias/cypherx-byok-<env>`; gateway IRSA role grants
    secretsmanager:GetSecretValue on `arn:aws:secretsmanager:*:*:secret:cypherx/byok/*`
    only. Disabled in `llms.secret_backends` until the infra phase provisions AWS;
    enabling it is a registry-row flip + resolver module, not a schema change.
  - Key rotation (backend-agnostic): tenant submits a new key via the same endpoint;
    old secret marked `pending-deletion` with 7-day grace; resolver prefers `current`
    over `pending-deletion`.
```

---

### Component 9 — Semantic Response Cache 📋

**What it is:** Cache LLM responses for identical or near-identical prompts. Saves cost and reduces latency.

**Cache key (CRITICAL — must include `tenant_id` to prevent cross-tenant data leak):**

```
Key:   llm-cache:{tenant_id}:{model}:{SHA256(serialised_messages_json + serialised_tools_json + temperature + top_p + max_tokens + response_format)}
Value: serialised UnifiedChatResponse + cache_metadata { stored_at, hit_count }
TTL:   configurable per tenant (default: 3600s)
```

> **Why `tenant_id` is mandatory in the key.** Two tenants sending semantically identical prompts (e.g., "summarize this email: ...") MUST NOT share a cache entry — the prompt itself may contain tenant-confidential data, and an attacker could deliberately probe for cache hits to exfiltrate another tenant's data. The cache key MUST be tenant-scoped from day one of design; this is a security invariant, not an optimization. CI lint rejects any code path that constructs a `llm-cache:*` key without `tenant_id` as the first segment after the namespace.

> **Why all sampling/output knobs are in the hash.** Temperature, top_p, max_tokens, and response_format change the response. Two requests differing only in `temperature` are NOT cache-equivalent. Tools list is hashed because identical messages with different tool sets produce different responses.

**Per-tenant cache toggle:** `llms.tenant_config.cache_enabled` (default `true` for `pro`/`enterprise`; default `false` for `free` to avoid cost-of-cache > cost-of-call on low-volume tenants).

**Cache bypass:** request field `"cache": false` skips cache lookup and write. Plus an `Authorization` header marker `X-Cypherx-No-Cache: true` for emergency bypass at the platform level.

**Cache invalidation:** every tenant has a `cache_epoch` integer (in `tenant_config`); incrementing it makes all old keys irrelevant. The key shape becomes `llm-cache:{tenant_id}:{cache_epoch}:{model}:{hash}`. Used after policy changes, model alias updates, or tenant-initiated "purge cache" from the dashboard.

**Cache hit accounting:** on a cache hit, write a usage_records row with `cached_prompt_tokens=prompt_tokens`, `prompt_tokens=0`, `cost_usd=cost_of_cache_lookup` (effectively zero) — so dashboards show the tenant's cache savings.

**Cost controls (prevent the cache from becoming an unbounded Valkey memory sink and from evicting idempotency/rate-limit keys it shares the Valkey instance with):**

- **Max cached value size: 64 KiB per entry.** Responses larger than the cap are NOT cached — lookup is hit-or-miss only; write is skipped. Counter `llms_cache_oversized_skipped_total{model}`. Rationale: cached responses run alongside idempotency bodies (32 KiB cap) and rate-limit counters — a single 1 MiB completion can evict thousands of small keys.
- **Per-tenant memory budget:** `pro` = 256 MiB, `enterprise` = 1 GiB, `free` cache-disabled (existing rule). Tracked via per-tenant `MEMORY USAGE` aggregate refreshed every 60 s into `llms.tenant_cache_stats`. On overshoot, oldest-by-LRU keys for that tenant are evicted before new writes. (Per-tenant strict LFU via Redis ACL-scoped DBs is 📋; first cycle uses key-prefix `SCAN` + lazy eviction.)
- **Global Valkey memory budget allocation (documented, alarmed):** `maxmemory-policy allkeys-lru` with allocation — idempotency 20 %, semantic cache 50 %, rate-limit + plan + multimodal-fetch counters 10 %, headroom 20 %. SRE alert if any segment exceeds its share for 5 min.
- **Valkey-outage fail-open (parity with rate limiter and idempotency):** on Valkey unreachable, cache lookup treated as miss; writes dropped. Counter `llms_cache_skipped_total{reason="valkey_unavailable"}`. No 5xx — falls through to the provider call.
- **Negative-cache guard:** errored completions (`status != success`, any 4xx/5xx, mid-stream errors) are NEVER cached. Cache writes happen only after a fully successful, fully-streamed response.
- **No caching of tool-call responses (📋 to revisit):** First cycle skips cache writes when the response contains `tool_calls[]`. Tool calls are typically part of a multi-turn loop where the next turn's input changes anyway — caching them is mostly wasted writes and complicates invalidation semantics around tool results.

---

### Component 10 — Budget & Quota Alerts 📋

**PostgreSQL (`llms.budgets`):**
```sql
CREATE TABLE llms.budgets (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      UUID NOT NULL UNIQUE,
  monthly_cap_usd NUMERIC(10,2),
  alert_at_pct   INTEGER DEFAULT 80,   -- alert when 80% consumed
  hard_stop      BOOLEAN DEFAULT false, -- true = block at 100%
  current_month  VARCHAR(7),           -- "2026-05"
  consumed_usd   NUMERIC(10,4) DEFAULT 0
);
```

**Budget enforcement consumer:**
```
Consumes: cypherx.llms.request.completed
→ Increment consumed_usd for tenant
→ If consumed_usd >= alert_at_pct% of monthly_cap_usd:
     Publish: cypherx.llms.budget.alert (warning)
→ If hard_stop = true AND consumed_usd >= monthly_cap_usd:
     Set tenant status = budget_exceeded in Valkey
     Next request from this tenant → 402 BUDGET_EXCEEDED
```

---

### Compose-Parity Runtime (first cycle — AMENDED, see Amendment Log)

The first-cycle runtime is **docker compose + Neon (Postgres) + Valkey + Redpanda + MinIO**.
There is NO K8s, Kong, Istio, Doppler, AWS, or Argo in the first cycle. The K8s spec below is
the **deploy-target (cloud) form**, conditional on the infra phase. Compose equivalents:

- **Service:** one `llms-gateway` compose service; same image, env-driven config.
- **Config/secrets:** every env var below is supplied via compose `.env` / environment blocks
  (the "from Doppler" list maps 1:1 to plain env vars; Doppler injection is the cloud form).
  `AUTH_JWKS_URL` / `AUTH_SERVICE_URL` point at compose service DNS (e.g. `http://auth:8080/...`)
  instead of cluster DNS.
- **Kafka topics:** an idempotent `topics-init` compose job (`rpk topic create` against Redpanda,
  safe to re-run) stands in for Terragrunt-provisioned topics. Topic names/partitions are identical.
- **Billing-replay journal:** compose named volume mounted at `/var/lib/llms-gateway/billing-replay/`
  (PVC in the cloud form).
- **Probes:** the same `/livez` / `/readyz` endpoints wired as compose `healthcheck`s.
- **Canary / PDB / NetworkPolicy / CronJob:** deploy-target mechanisms — no compose equivalent
  required first cycle. The synthetic provider canary's compose stand-in is a scheduled smoke
  script (CI schedule or cron sidecar) firing the same 10-token completion.
- **Scheduled jobs** (outbox cleanup nightly job, pricing-staleness check): compose cron sidecar
  or CI scheduled pipeline first cycle; K8s CronJob in the cloud form.

### K8s Deployment Spec (deploy-target / cloud form — conditional on the infra phase)

```yaml
Namespace:   shared-core
Deployment:  llms-gateway
Replicas:    min 2, max 15 (HPA on CPU 70% — first-cycle minimum)
Node selector: node-role: core

Resources:
  requests: { cpu: 500m, memory: 768Mi }
  limits:   { cpu: 2000m, memory: 2Gi }    # bumped from 1Gi: streaming buffers
                                            # + multimodal payloads + tool-call
                                            # aggregation easily exceed 1Gi.

Health probes (Contract 7 — post-edit):
  livenessProbe:  GET /livez  (initialDelay: 10s, period: 10s)
                  Process-only; NEVER touches DB/Valkey/Kafka/providers.
  readinessProbe: GET /readyz (initialDelay: 5s, period: 5s)
                  Hard dependencies (fail readiness): PostgreSQL connectivity.
                  Soft dependencies (log + metric only): Valkey, Kafka,
                    provider APIs (failing readiness on a provider outage would
                    yank the gateway from LB and break other-provider tenants).

Env vars (env-driven — compose `.env` first cycle; Doppler-injected in the cloud form):
  DATABASE_URL          (PgBouncer → llms schema, runtime user llms_user)
  VALKEY_URL
  KAFKA_BROKERS
  KAFKA_SASL_PASSWORD
  AUTH_JWKS_URL         (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  AUTH_SERVICE_URL      (http://auth-service.shared-core.svc.cluster.local:8080)
  SERVICE_BOOTSTRAP_SECRET   (Contract 12; from service-auth/llms-gateway/bootstrap_secret)
  ANTHROPIC_API_KEY     (platform-managed)
  OPENAI_API_KEY        (platform-managed)
```

> **JWKS verification (Auth integration — applies to every Phase 3+ service):**
> All services verify incoming JWTs locally against the Auth service's JWKS document.
> - URL is env-driven (`AUTH_JWKS_URL`): compose service DNS first cycle (`http://auth:8080/.well-known/jwks.json`); in-cluster URL in the cloud form: `http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json` (NEVER go through ALB for internal calls — adds latency, breaks during ALB outages).
> - JWKS cache TTL: 5 minutes.
> - On `kid` cache miss, refresh JWKS at most once per minute (Contract 1).
> - Internal DNS keeps the path inside the private network (mTLS via Istio in the cloud form); no service token needed for this read (the JWKS document is public material).

> **HPA on CPU is a first-cycle minimum.** LLM gateway pods are I/O-bound on provider
> streams — CPU stays low while open SSE connections pile up. 📋 follow-up: add a KEDA
> Prometheus-based scaler on `llms_active_requests_per_pod > 50` or
> `llms_p95_latency_seconds > 5`.

> **Blast-radius mitigations (the gateway is the platform's single LLM choke point — every Phase 4+ service routes through it, so a bad deploy = total LLM outage. AMENDED scoping: code-side mitigations are MANDATORY first cycle except where marked 📋; K8s-mechanism items (canary, PDB) are deploy-target — cloud form, conditional on the infra phase, with compose stand-ins per the Compose-Parity subsection):**
> - **Canary deploy via Argo Rollouts:** new revision receives 5 % of traffic for 10 min; if `llms_5xx_rate < 0.5 %` AND `p95 latency < 1.5×` baseline AND `llms_billing_write_failed_total` flat, auto-promote to 50 % for 10 min, then 100 %. Otherwise auto-rollback.
> - **PodDisruptionBudget:** `minAvailable: 50 %`. No more than half the fleet may be voluntarily disrupted at once (node drain, version upgrade).
> - **Required client-side circuit breaker (Contract addendum):** every Phase 4+ service calling the gateway MUST wrap calls in a circuit breaker (open after 5 consecutive 5xx in 10 s; half-open probe every 30 s). On open circuit, the caller degrades gracefully — RAG falls back to keyword retrieval, xAgent surfaces "LLM unavailable" rather than spinning, Guardrails fails-closed. This contract is added to `phase-00-contracts.md` (Contract 20) so it's enforced everywhere, not just remembered here.
> - **No emergency direct-provider bypass.** Considered and rejected. A bypass path forces every service to maintain a fallback provider client + key + cost-attribution path, defeating the gateway's reason for existing. The mitigation is "make the gateway extremely reliable" via the bullets above — not "let services route around it."
> - **Per-tier replica pools (📋 Phase 13 hardening):** split into `llms-gateway-platform` (internal traffic: xAgent, Guardrails, RAG, Memory) and `llms-gateway-external` (third-party developer traffic via Kong) so a runaway external tenant cannot evict internal capacity via Valkey/Postgres pool contention. Same image, separate Deployments, separate HPAs, shared Postgres/Valkey/Kafka.
> - **Synthetic provider canary:** a CronJob fires a 10-token completion against each provider every 60 s through the gateway's full happy path (auth → adaptor → outbox → stream). Failures emit `llms_synthetic_failed_total{provider}`; alerts page before real users notice.

---

## ⚡ First Cycle Implementation Checklist

> **WP06 — LLMs pre-work package (AMENDED: declared dependency of Phases 5/6 — see Amendment Log).**
> The ⚡ surfaces tagged **(WP06)** below gate the RAG (Phase 5) and Memory (Phase 6) builds, which
> hard-depend on them: `POST /v1/embeddings` (256-item / 25 MiB caps, mock embeddings provider for
> tests, usage_records + outbox), `GET /v1/models` returning `embedding_dim`, the `embed` alias +
> embedding pricing seeds, and Valkey Idempotency-Key replay. **RAG/Memory builds may NOT start
> ahead of WP06 completion.**

> **Checklist split (AMENDED — see Amendment Log):** "Service code" items are buildable in the
> actual first-cycle runtime (compose + Neon + Valkey + Redpanda + MinIO). "Deploy-target" items
> are the cloud (K8s/Argo) form, conditional on the infra phase, with compose equivalents
> documented in the Compose-Parity subsection.

### Service code (compose-buildable)

- [ ] Service architecture planned separately
- [ ] Unified chat completion endpoint (`POST /v1/chat/completions`) — non-streaming
- [ ] **Unified schema supports `tools[]`, `tool_choice`, and `tool_calls` in messages/response** (required by xAgent execution loop)
- [ ] **Multimodal `image_url` content blocks supported in user messages** — default is **URL pass-through** to the provider (amended; stale SSRF premise — see Amendment Log). Config-gated fetcher (`MULTIMODAL_FETCHER_ENABLED`, default `false`); when enabled it MUST implement the full SSRF-hardened ruleset: HTTPS-only, resolve-once + IP-pin, IP denylist (RFC1918/link-local/metadata/CGNAT/multicast), redirect re-check (3 hops), 20 MiB streaming size cap, MIME sniff vs header match, decompression-bomb guard (10× ratio), 5/5/15-s timeouts, per-tenant 100 fetches/min via Valkey, `cypherx.llms.multimodal_fetch` audit event (egress NetworkPolicy → deploy-target section)
- [ ] **Request `metadata` rejects the full reserved set** (`agent_id`, `tenant_id`, `trace_id`, `span_id`, `request_id`, `task_id`, `user_id`, `org_id` — pulled from JWT/headers only, Contract 13). Reserved-set list lives in `contracts/api/reserved-metadata-keys.md`.
- [ ] Body size override: 25 MiB on chat/embeddings routes
- [ ] Embeddings input array capped at 256 items (WP06)
- [ ] Streaming support (`POST /v1/chat/completions` with `stream: true`) with per-provider usage normalization
- [ ] **Anthropic adaptor normalizes `tool_use` → `tool_calls` and `stop_reason: "tool_use"` → `finish_reason: "tool_calls"`**
- [ ] **Anthropic adaptor populates `cached_prompt_tokens` and `cache_creation_tokens` from `usage.cache_read_input_tokens` / `cache_creation_input_tokens`**
- [ ] **OpenAI adaptor force-sets `stream_options: { include_usage: true }` on every streaming call (override client supplied)**
- [ ] **Unified request schema reserves `stream_options.aggregate_tool_calls` (default `true`) and `parallel_tool_calls` (default `true`)** — `aggregate_tool_calls: false` accepted in schema, rejected at runtime with 422 `FEATURE_NOT_YET_IMPLEMENTED` (delta passthrough is 📋 Phase 12). This reserves the wire-format switch so future delta-mode is a flag flip, not a breaking change.
- [ ] **Tool-call streaming aggregated server-side** (server emits one final `data: { "type": "tool_calls", ... }` event)
- [ ] **Provider normalization gaps closed:** temperature clamp `[0,2]` → `[0,1]` on Anthropic with `X-Cypherx-Param-Clamped` header; per-model `max_tokens` ceiling enforced before dispatch (400 `MAX_TOKENS_EXCEEDED`) **from `llms.model_capabilities` — DB-authoritative, never an in-code map (amended)**; tool-name regex `^[a-zA-Z0-9_-]{1,64}$` validated at schema; `parallel_tool_calls` mapped to Anthropic `disable_parallel_tool_use`; refusal `stop_reason: "refusal"` mapped to unified `content_filter`
- [ ] **Mid-stream provider error emits `event: error` + records partial usage_record + outbox event**
- [ ] Embeddings endpoint (`POST /v1/embeddings`) — accepts batched input (≤256), 25 MiB body cap, **mock embeddings provider for tests**, writes usage_records + outbox like chat (WP06)
- [ ] Models list endpoint (`GET /v1/models`) — OpenAI-compatible shape, per-tenant filtering, **returns `embedding_dim` for embedding models** (WP06)
- [ ] Anthropic provider adaptor
- [ ] OpenAI provider adaptor
- [ ] Model alias resolution (platform defaults seeded **in the seed migration itself**, incl. `embed`/`code`/`vision` — audit-found drift reconciled; `embed` alias is WP06); mixed-scope RLS pattern applied
- [ ] **`llms.model_capabilities` table created + seeded** (max_tokens, modalities, provider, `embedding_dim`) — pulled into Phase 3 (amended); **DB is the single authority for aliases/pricing/capabilities: startup-blocking load + 60 s periodic refresh; in-code maps are test fixtures only**; opus model id reconciled (`claude-opus-4-7` → `claude-opus-4-8` drift)
- [ ] **Dual-mode auth (Contract 12)**: internal mode (service-JWT + `X-Forwarded-Agent-JWT`) AND external mode (bare agent/api-key JWT issued by `AUTH_ISSUER_URL`). Same downstream code path; gateway middleware unifies tenant_id derivation.
- [ ] **JWKS fetch via env-driven `AUTH_JWKS_URL`** (compose service DNS first cycle, e.g. `http://auth:8080/.well-known/jwks.json`; cluster DNS `http://auth-service.shared-core.svc.cluster.local:8080/...` is the cloud form), 5-min cache, refresh-on-`kid`-miss rate-limited 1/min
- [ ] JWT scope verify locally (`llm:invoke` for both chat AND embeddings); **plan tier read from the Auth-issued `plan` JWT claim (Auth limits endpoint as fallback), cached 60 s in Valkey** (amended — Auth interim-owns `tenants.plan`); call Auth `/authorize` only for remaining tenant-level checks (budget hard-stop)
- [ ] Token counting and cost calculation (explicit formula from Component 1)
- [ ] **`llms.provider_pricing` table created + seeded with current Anthropic + OpenAI rates** (including cached + creation costs) **+ embedding-model pricing rows (WP06)**; staleness alert if rates >60 days stale (compose first cycle: CI scheduled check; weekly cron Slack alert is the cloud form)
- [ ] **Usage record `usage_records` written with gateway-minted `llm_call_id` (UNIQUE `(tenant_id, llm_call_id)` — THE billing uniqueness key, amended) + `request_id` (NON-unique correlation) + `duration_ms` + NON-NULL `trace_id` (Contract 5/8 field names); PK is UUID. `request_id` = inbound `X-Request-ID`; `trace_id` = parsed from `traceparent`. Gateway never writes NULL — synthesise + WARN if header absent. Hot-path INSERT fails loudly on duplicate `llm_call_id` (no ON CONFLICT).**
- [ ] **`usage_records` carries `api_key_id` + `principal_type`** so external developers see per-key usage breakdowns (`GET /v1/usage?group_by=api_key_id`)
- [ ] **`llms.api_key_acls` ONLY, keyed by Auth's `api_key_id` (per Contract 18)** — per-key allowlists on `model`, `alias`, `provider_key`. External developer can restrict an API key to `gpt-4o-mini` only. **Auth owns API keys — LLMs MUST NOT create a second `api_keys` table (amended).**
- [ ] **`cypherx.llms.usage.recorded` alias topic per Contract 19** — same payload as `request.completed`; downstream metering joiners subscribe to the canonical name
- [ ] **BYOK (Component 8) ⚡ promoted** — pluggable secret backend (amended): `llms.secret_backends` registry + `sealed:v1` backend (raw key AES-256-GCM envelope-encrypted in Postgres under env-supplied `LLMS_BYOK_KEK`, `llms.sealed_secrets` table) + tenant-CRUD endpoint + key rotation grace. (`secretsmanager:` AWS backend = optional cloud form, registry-disabled — see 📋)
- [ ] **Semantic cache key MUST include `tenant_id` as first segment** — `llm-cache:{tenant_id}:{cache_epoch}:{model}:{hash}`; CI lint rejects keys without tenant_id; per-tenant `cache_enabled` toggle; per-tenant `cache_epoch` for invalidation
- [ ] **`GET /v1/usage` and `GET /v1/cost`** endpoints expose per-tenant aggregations groupable by `model`, `agent_id`, `api_key_id`, `date` — **⚡ is the sole owner of these aggregation endpoints (amended; the duplicate 📋 line is deleted — 📋 keeps only dashboard extensions)**
- [ ] **Outbox pattern (`llms.outbox`) — usage_record + outbox row in same transaction; separate publisher loop with DLQ after 10 attempts**
- [ ] **Billing-write failure journal** at `/var/lib/llms-gateway/billing-replay/{llm_call_id}.json` (durable volume — compose named volume first cycle; PVC in the cloud form); startup + 30 s replay worker drains into Postgres keyed on `(tenant_id, llm_call_id)` — **the replay worker is the ONLY writer allowed `ON CONFLICT DO NOTHING` (amended)**; readiness fails at journal depth > 1 000; gateway 5xx with `BILLING_DEGRADED` at depth ≥ 10 000. Provider-success responses always served (with `X-Cypherx-Billing-Pending: true` header) — never 5xx the client after we already burned their tokens.
- [ ] **Idempotency-Key support on `POST /v1/chat/completions` (and `/v1/embeddings`) — Valkey-backed, 24h TTL, replay returns cached body with `Idempotent-Replayed: true`, in-flight returns 409 `IDEMPOTENCY_REQUEST_IN_FLIGHT` (Contract 2 `IDEMPOTENCY_*` spelling, amended); `stream=true` requests record the key but are REPLAY-EXEMPT (`Idempotent-Replayed: false`, amended); fail-open on Valkey outage** (WP06)
- [ ] **Kafka event payload aligned with Contract 5 first-cycle spec** (includes BOTH `llm_call_id` + `request_id` (amended), `duration_ms`, `cached_prompt_tokens`, `cache_creation_tokens`); partition key = `tenant_id`
- [ ] **`provider.key_ref` registry-driven format (amended)** — `<backend>:<ref>` validated app-layer against `llms.secret_backends` (replaces the literal CHECK constraint); **`env:<VAR>` refs read pre-injected env vars (no runtime secret-store fetch; unset var = fatal boot error); `sealed:v1:<uuid>` refs decrypt from `llms.sealed_secrets` with 15-min in-process cache; `secretsmanager:` reserved as a registry-disabled future backend**
- [ ] **Anthropic adaptor rejects `response_format: json_object | json_schema` with 422 `MODEL_UNSUPPORTED`** (no silent prompt-engineering fallback); `system` role messages translated to Anthropic top-level `system` field
- [ ] **`llms.rate_limits` table created + seeded** (`free`/`pro`/`enterprise` plan rows); tenant plan read from the **Auth-issued `plan` claim** (amended) with 60s Valkey cache
- [ ] Basic rate limiting (fixed window, per-tenant, using Valkey) — **decided semantics (amended): `requests_per_min` pre-flight check + `tokens_per_min` post-hoc window debit from ACTUAL usage (predictive token pre-check is 📋)**; **fail-open with telemetry on Valkey outage**
- [ ] **`/livez`, `/readyz`, `/metrics`** endpoints — readiness gated on PostgreSQL only; Valkey/Kafka/providers are soft
- [ ] Atlas migrations (Contract 14) for `llms.*` schema (providers, secret_backends, sealed_secrets, model_aliases, model_capabilities, usage_records, outbox, provider_pricing, rate_limits — DDLs in this phase doc — plus `llms.api_key_acls` per Contract 18; no other schema)
- [ ] RLS policies applied: tenant-scoped (`usage_records`, `sealed_secrets`); mixed-scope `OR tenant_id IS NULL` (`providers`, `model_aliases`)
- [ ] **Idempotent `topics-init` compose job** (`rpk topic create`, safe to re-run) provisions the `cypherx.llms.*` topics — Terragrunt stand-in per the Compose-Parity subsection (amended)
- [ ] **Contract 20 (new — added to `phase-00-contracts.md`): client-side circuit breaker MANDATORY for every gateway caller** — open after 5 consecutive 5xx in 10 s; half-open probe every 30 s; caller must degrade gracefully on open circuit
- [ ] Runs in the first-cycle compose stack (gateway service + healthchecks + named volume for the billing-replay journal) — see Compose-Parity subsection

### Deploy-target (cloud form — conditional on the infra phase; compose equivalents in the Compose-Parity subsection)

- [ ] Deployed to K8s (shared-core namespace) via ArgoCD (compose first cycle: service in the stack — see above)
- [ ] **Argo Rollouts canary deploy:** 5 % → 50 % → 100 % progression gated on `llms_5xx_rate < 0.5 %`, `p95 latency < 1.5× baseline`, and `llms_billing_write_failed_total` flat; auto-rollback otherwise (no compose equivalent required first cycle)
- [ ] **PodDisruptionBudget `minAvailable: 50 %`** on `llms-gateway` Deployment (no compose equivalent)
- [ ] **Synthetic provider canary CronJob** — 10-token completion per provider every 60 s through the full gateway path; alerts on `llms_synthetic_failed_total{provider}` (compose stand-in: scheduled smoke script via CI schedule or cron sidecar)
- [ ] **Egress NetworkPolicy** for the multimodal fetcher namespace (fetcher is config-disabled in compose, so this lands with the fetcher's cloud enablement)

## 📋 Full Enterprise Implementation Checklist

- [ ] Smart routing (cheapest / fastest / most capable)
- [ ] Provider fallback chain
- [ ] All remaining providers (Gemini, Groq, Azure OpenAI, Mistral, Ollama, Bedrock)
- [ ] BYOK `secretsmanager:` backend (AWS Secrets Manager + per-tenant KMS CMK + IRSA — cloud form): flip the `llms.secret_backends` registry row + add the resolver module. (BYOK registration/management itself is ⚡ via the `sealed:v1` backend — amended, see Amendment Log)
- [ ] Anthropic prompt-caching annotation (`cache_control: { type: "ephemeral" }`) exposed in unified schema
- [ ] Semantic response caching in Valkey (rename Component 9 or implement embedding-similarity) — MUST include first-cycle-documented cost controls: 64 KiB max-value-size, per-tenant memory budget (256 MiB / 1 GiB by plan), Valkey segment allocation (cache 50 %, idempotency 20 %, counters 10 %, headroom 20 %), fail-open on Valkey outage, negative-cache guard (no caching errored or `tool_calls[]` responses)
- [ ] Per-tier replica pool split: `llms-gateway-platform` (internal) vs `llms-gateway-external` (Kong-fronted third-party), separate HPAs sharing one Postgres/Valkey/Kafka
- [ ] Extract `llms-billing` sibling service (budget tracker + provider-pricing) — Kafka-consumer side only, off the request hot path. Coupling-watch debt from Phase Overview.
- [ ] Tool-call streaming pass-through (delta-mode) for interactive UI clients
- [ ] Budget tracking consumer (reads from Kafka)
- [ ] Budget alerts (`cypherx.llms.budget.alert`)
- [ ] Hard budget stop enforcement
- [ ] Per-agent rate limits (in addition to per-tenant)
- [ ] Predictive token rate limiting (estimate-before-dispatch) — the post-hoc `tokens_per_min` window debit is already ⚡ (amended, see Amendment Log)
- [ ] Provider health monitoring (test calls, latency tracking)
- [ ] Usage dashboard EXTENSIONS only — richer reporting on top of the ⚡ `GET /v1/usage` / `GET /v1/cost` aggregation endpoints (the endpoints themselves moved to ⚡; duplicate line deleted — see Amendment Log)
- [ ] PII masking in request/response logs
- [ ] Pre/post request interceptor middleware pipeline
- [ ] KEDA Prometheus-based HPA on `llms_active_requests_per_pod` and `llms_p95_latency_seconds`
