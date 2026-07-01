# Phase 14 — SDKs
> **Status:** ⏳ Pending | **Depends On:** Phase 13 (APIs stable) | **Blocks:** —
> **First Cycle:** 📋 Not required. Built after APIs are fully stable and documented.

---

## Phase Overview

SDKs are built **last** because SDK design must mirror stable APIs. A premature SDK locks the API shape and creates migration debt. After Phase 13, the APIs are stable, documented, and security-hardened — the right moment for SDKs.

> **Per-service SDK packaging (NEW — implements the "Externally Operable" principle at the SDK tier).**
>
> Each SharedCore service ships its OWN per-language client package. A meta-package (`cypherx-ai` / `@cypherx-ai/sdk`) bundles them all for the platform user who wants everything; per-service packages let a developer install ONLY the SharedCore service they want to use without pulling in the whole platform surface.
>
> | Service | Python package | npm package |
> |---------|---------------|-------------|
> | Auth | `cypherx-auth` | `@cypherx-ai/auth` |
> | LLMs | `cypherx-llms` | `@cypherx-ai/llms` |
> | Guardrails | `cypherx-guardrails` | `@cypherx-ai/guardrails` |
> | RAG | `cypherx-rag` | `@cypherx-ai/rag` |
> | Memory | `cypherx-memory` | `@cypherx-ai/memory` |
> | Tools (MCP client for testing) | `cypherx-tools` | `@cypherx-ai/tools` |
> | Skills | `cypherx-skills` | `@cypherx-ai/skills` |
> | xAgent | `cypherx-xagent` | `@cypherx-ai/xagent` |
> | Workflows / A2A delegation | `cypherx-workflows` | `@cypherx-ai/workflows` |
> | **Meta-package (everything)** | `cypherx-ai` | `@cypherx-ai/sdk` |
>
> Each per-service package shares one **core runtime** package (`cypherx-core` / `@cypherx-ai/core`) providing: OIDC discovery (Contract 1), JWT verification, idempotency-key generation, rate-limit-header back-off, retry, signed-bundle validation. Per-service packages depend on it; the meta-package depends on all sub-packages but ships no logic of its own.
>
> **Why split:** an external developer who only needs Memory shouldn't carry xAgent's surface; an enterprise customer adopting Guardrails as a standalone product shouldn't import LLM provider clients. SDK packaging mirrors service packaging — single principle, no exceptions.

**Deliverable:** Python and TypeScript SDKs covering xAgent, SharedCore services, and MCP client helpers. Type-safe, well-documented, with examples. **Per-service packages + meta-package** in both languages.

> 🏗️ **Service Architecture Note:** SDK architecture (code generation strategy, typing approach, streaming implementation, retry handling) must be planned separately per language before implementation begins.

---

## High Level Design

### SDK Coverage (versioned by SDK release; not everything ships in v0.1)

```
SDK v0.1 (first release — aligned with Phase 9A APIs)
├── xAgent Client
│   ├── create_agent()            → two-step: Auth POST /v1/agents + xAgent /runtime
│   ├── submit_task()             → task response (sync only)
│   ├── get_task()                → task status + result
│   ├── cancel_task()             → DELETE /v1/tasks/{id}
│   └── tools.list_for_agent()    → resolves agent.allowed_tools against Tool Registry
│                                   (read-only — tools are invoked by xAgent, not the SDK)
│
├── SharedCore Clients
│   ├── AuthClient
│   │   ├── register_agent()
│   │   ├── issue_key()
│   │   └── (issue_token is SDK-INTERNAL — see "Auth model" below)
│   ├── LLMsClient
│   │   ├── chat()                → unified completion (non-streaming)
│   │   └── embed()               → embedding vector
│   ├── GuardrailsClient
│   │   ├── check_input()
│   │   └── check_output()
│   └── (Memory + RAG SDK clients land in v0.2 — services themselves are 📋 in first cycle)

SDK v0.2 (lands AFTER xAgent streaming + Memory + RAG land in production)
├── xAgent.stream_task()          → async iterator of SSE events (Phase 9 post-edit:
│                                   mode=stream is 📋; SDK can't ship until xAgent does)
├── LLMsClient.stream_chat()      → async iterator
├── MemoryClient                  → store / retrieve
├── RAGClient                     → ingest / query
└── tools.test_invoke()           → direct tool invocation for TESTING ONLY;
                                    requires platform:admin scope

SDK v0.3 (lands AFTER Phase 10 + Phase 13 Domain 7 SPIFFE migration complete)
├── xAgent.submit_workflow()      → workflow response (DAG decomposition)
├── xAgent.get_workflow()         → status + subtask graph
└── xAgent.cancel_workflow()      → triggers Phase 10 cancel fan-out

NOT in any external SDK (deliberate exclusions):
  - MCP Client Helper / invoke_tool() — tools are invoked by xAgent on behalf of an
    agent, NOT by external SDK users. Users are not agents; they don't speak MCP.
    (test_invoke above is the only exception, gated by platform:admin.)
  - A2A Client Helper — A2A is agent-to-agent, invoked by xAgent's internal pipeline
    when an agent delegates to another agent. External SDK users never originate A2A.
```

---

## Low Level Design

> All items are 📋 — none required for first cycle.

---

### Component 1 — Python SDK 📋 (P1)

```
Package: cypherx-ai          (also reserve `cypherx` alias on PyPI)
PyPI:    pypi.org/project/cypherx-ai

Install:
  pip install cypherx-ai

Usage (recommended — env vars):
  # Set both:
  #   CYPHERX_API_KEY=cx_prod_...
  #   CYPHERX_AGENT_ID=<uuid>
  # in environment or .env file.
  import os
  from cypherx_ai import CypherXClient

  client = CypherXClient()                           # reads CYPHERX_API_KEY
  agent  = client.agents(os.environ["CYPHERX_AGENT_ID"])

  # Submit a task (sync; v0.1)
  task = agent.submit_task(message="Summarise the latest AI news")
  print(task.output.message)
  print(task.trace_id)                                # for log correlation

  # Async client (asyncio) with context manager
  async with AsyncCypherXClient() as client:
      agent = client.agents(os.environ["CYPHERX_AGENT_ID"])
      task  = await agent.submit_task(message="...")

  # Streaming (v0.2 — after xAgent mode=stream lands; NOT in v0.1)
  async for event in agent.stream_task(message="..."):
      if event.type == "token":
          print(event.content, end="", flush=True)

Constructor (full signature):
  CypherXClient(
      api_key:        str | None = None,                     # default: CYPHERX_API_KEY env var
      base_url:       str        = "https://api.cypherx.ai", # override for staging / on-prem
      timeout:        float      = 30.0,
      max_retries:    int        = 3,
      http_client:    httpx.Client | None = None,            # inject for corp proxy / custom CA
      idempotency_key_generator: Callable[[], str] | None = None,
      # telemetry intentionally absent — see "Telemetry" below
  )

Python version: 3.10+
Type hints: full mypy-compatible type annotations
Async: asyncio support (AsyncCypherXClient) with proper context-manager cleanup
Models: Pydantic v2 for all request/response types (auto-generated from OpenAPI)
HTTP: httpx (sync + async share connection pool)
Retry: tenacity-based with the per-method policy in Component 6
```

> **Auth model (CRITICAL — Contract 1 / Phase 2 flow, not "API key is the wire credential"):**
> Per Contract 1 (post-edit) and Phase 2: API keys exchange for short-lived JWTs (1h max TTL); all API calls carry `Authorization: Bearer <jwt>`, NOT the raw API key. The SDK hides this:
> 1. On first call (or on cold start without a non-expired cached JWT), SDK calls
>    `POST /v1/agents/{agent_id}/token` with body `{ "api_key": "<cx_...>" }`. The
>    `api_key` field in the request body is the ONLY authentication on this single
>    endpoint — no `Authorization` header on this one call. Every subsequent call
>    uses the returned JWT.
> 2. Response JWT cached in process memory; refreshed 5 min before `exp`.
> 3. All subsequent API calls send the JWT in `Authorization: Bearer <jwt>`.
> 4. The raw API key NEVER appears on the wire after the token-exchange call —
>    no header, no log line, no error message.
> 5. On `401 UNAUTHORIZED` from any endpoint: SDK invalidates the cached JWT,
>    re-exchanges once; on second 401 surfaces `CypherXAuthError`.
> 6. Verify the body-field shape against Phase 2's `/v1/agents/{id}/token` spec
>    at SDK build time (OpenAPI types are generated from Phase 2's spec, so any
>    drift surfaces as a compile error in the SDK before release).

> **Agent identifier — UUID ONLY (canonical):**
> `client.agents(agent_id)` accepts a UUID string. Names are NOT accepted in this
> position because resolving "name → UUID" via `GET /v1/agents?name=...` requires
> `tenant:admin` scope (Phase 2 list endpoint) — but the typical SDK credential is
> an AGENT API key which holds only that agent's scopes. Name resolution would
> silently fail with 403 the first time a normal developer uses it.
>
> Recommended usage:
>
>   client = CypherXClient()                                # CYPHERX_API_KEY env var
>   agent  = client.agents(os.environ["CYPHERX_AGENT_ID"])  # UUID env var
>   task   = agent.submit_task(message="...")
>
> Two env vars together = SDK is fully configured for one agent. The pair
> (`CYPHERX_API_KEY`, `CYPHERX_AGENT_ID`) is the standard 12-factor shape; both
> should be set together.
>
> Convenience for multi-agent apps:
>   client.agent_by_id(uuid)   → identical to client.agents(uuid); explicit for code review
>
> Listing agents for human-readable UIs (NOT for SDK auto-resolution):
>   client.agents.list(name="my-agent")
>   → returns AgentSummary objects with .id, .name, .status. REQUIRES tenant:admin
>     scope (will 403 with normal agent API keys). Intended for dashboard / admin
>     workflows where the caller is human and the credential is a tenant-admin key.
>
> If you want SDK auto-resolution by name in a future release, Phase 2 needs to add
> `GET /v1/agents/by-name/{name}` scoped to `agent:read` against the named agent
> (so it works with normal agent API keys). Tracked as 📋 — not in v0.1.

---

### Component 2 — TypeScript SDK 📋 (P1)

```
Package: @cypherx-ai/sdk
npm:     npmjs.com/package/@cypherx-ai/sdk

Install:
  npm install @cypherx-ai/sdk

Two import paths (DO NOT mix):

  @cypherx-ai/sdk          → Node mode (default) — direct API access with API key
  @cypherx-ai/sdk/browser  → Browser mode        — BFF-only; NO direct API access

Node usage (server-side: Node 18+, Bun, Deno):
  import { CypherX } from '@cypherx-ai/sdk'

  // Recommended: env vars CYPHERX_API_KEY + CYPHERX_AGENT_ID
  const client = new CypherX()
  const agent  = client.agents(process.env.CYPHERX_AGENT_ID!)

  const task = await agent.submitTask({
    message: 'Summarise the latest AI news'
  })
  console.log(task.output.message, task.traceId)

Browser usage (Vite / Next.js client / etc.) — session-cookie + CSRF only:
  import { CypherX } from '@cypherx-ai/sdk/browser'

  const client = new CypherX({
    baseUrl:       '/cypherx',                  // path on customer's BFF
    credentials:   'include',                   // session cookie sent automatically
    getCsrfToken:  () => readCookie('csrf')     // double-submit per Phase 12
  })
  const agent = client.agents(/* agent_id received from BFF */)
  // NEVER pass apiKey or jwt in browser mode — type system rejects both.

TypeScript version: 5.0+
Types: generated from OpenAPI via openapi-typescript; Zod runtime validation optional.
Runtime: Node 18+ (node mode); modern browsers + Bun/Deno (browser mode).
ESM + CJS: dual package via `"exports"` field with subpath resolution.
```

> **Why two import paths — CRITICAL security boundary:**
> Putting an API key (`cx_prod_...`) or an agent JWT in browser JavaScript exposes it
> to anyone with DevTools — exactly the XSS vector Phase 12 went to great lengths to
> avoid with the BFF pattern. So:
>
> - **Node mode** (default): full direct-API access with API key. SDK handles the JWT
>   exchange flow internally (same as Python SDK Component 1).
> - **Browser mode** (`/browser` subpath): NO direct API access, NO API key parameter,
>   **NO JWT parameter** (the type system rejects both). Browser SDK talks ONLY to the
>   customer's own BFF (Next.js API routes, Hono server, etc.) following the Phase 12
>   BFF pattern verbatim: server-side session, httpOnly session cookie, JWT held
>   server-side, CSRF cookie double-submitted on mutations.
> - The browser SDK uses `credentials: 'include'` so the same-origin session cookie
>   is sent automatically. It reads a CSRF cookie (non-httpOnly) and double-submits
>   it via `X-CSRF-Token` header on every POST/PUT/PATCH/DELETE.
> - **CORS**: `api.cypherx.ai` rejects browser-originating requests (Phase 12 post-edit).
>   Even if a user copy-pastes the node-mode SDK into a browser, requests fail.
> - This eliminates "I shipped my API key/JWT to production by accident" — the type
>   system prevents it at compile time.

### Component 2b — Customer-BFF Protocol (the contract browser SDK expects) 📋

The browser SDK isn't magic — it makes HTTP calls to a known endpoint shape. Customers
must implement a BFF that exposes that shape. To keep customer BFFs interchangeable
and the SDK simple, the protocol is fixed:

**Required endpoints (the BFF MUST implement all of these to be SDK-compatible):**

```
Path prefix: customer-chosen (e.g. /cypherx, /bff/cypherx); SDK is configured with it.
Auth (every endpoint):
  - Session cookie validated server-side per Phase 12 (httpOnly, SameSite=Strict).
  - X-CSRF-Token header verified against the non-httpOnly csrf cookie on every
    POST/PUT/PATCH/DELETE.
  - On any mismatch / missing cookie → 401 (SDK surfaces CypherXAuthError).

Endpoints (mirror the Node-mode SDK surface 1:1):

  POST   <prefix>/agents/:id/tasks          → BFF proxies to xagent /v1/tasks
  GET    <prefix>/tasks/:id                  → BFF proxies to xagent /v1/tasks/:id
  DELETE <prefix>/tasks/:id                  → BFF proxies to xagent DELETE /v1/tasks/:id
  GET    <prefix>/agents/:id/tools           → BFF proxies to tool-registry list
  POST   <prefix>/llms/chat                  → BFF proxies to llms-gateway
  POST   <prefix>/llms/embeddings            → BFF proxies to llms-gateway
  POST   <prefix>/guardrails/check-input     → BFF proxies to guardrails
  POST   <prefix>/guardrails/check-output    → BFF proxies to guardrails

  (v0.2 additions:)
  GET    <prefix>/agents/:id/tasks/stream    → SSE proxy of xagent stream mode
  POST   <prefix>/memory/store               → BFF proxies to memory-service
  POST   <prefix>/memory/retrieve            → BFF proxies to memory-service
  POST   <prefix>/rag/knowledge-bases/:id/query → BFF proxies to rag-service

Each endpoint:
  - Forwards the originating request_id (or generates one) per Contract 8.
  - On the server side, BFF performs the api-key → JWT exchange ONCE per agent
    (cached per Phase 14 SDK auth model) and attaches the JWT in
    `X-Forwarded-Agent-JWT` to the platform call. BFF's own service JWT goes in
    `Authorization`.
  - BFF maps platform errors (Contract 2 shape) to HTTP status verbatim — no
    repackaging. The SDK's typed exception hierarchy depends on exact error
    codes and shapes.
  - BFF MUST NOT add or remove `traceparent` / `tracestate` / `X-Request-ID`
    headers on the outbound platform call — propagate them verbatim from the
    browser request so trace continuity holds.
```

**Reference implementation:** `examples/typescript/browser-bff/` ships a working
Next.js BFF following this protocol exactly. Customers fork it as their starting
point. Customers may implement in any language (Python/FastAPI, Go/gin, etc.) as
long as the wire protocol matches.

**Versioning:** the BFF protocol is versioned alongside the SDK. v0.1 ships a
minimal shape; v0.2 adds streaming + Memory + RAG endpoints. Browser SDK refuses
to call BFF endpoints introduced in a later version than the SDK was built
against (compile-time check via TypeScript types).

> **Why not just let the SDK call api.cypherx.ai with a JWT?**
> Phase 12 Kong CORS rule (`credentials: false`) blocks browser cookie-auth to
> api.cypherx.ai. Browser-side JWT in localStorage is the exact XSS vector this
> whole pattern exists to prevent. There is no Plan B — the BFF is mandatory
> for browser-side platform access.

---

### Component 3 — SDK Generation Strategy 📋

```
OpenAPI source-of-truth URLs:
  https://api.cypherx.ai/v1/openapi/<service>.json    ← per service (one per Phase 2–11)
  https://api.cypherx.ai/v1/openapi.json              ← AGGREGATED, generated by platform-mgmt CI
                                                        on every release, merges per-service specs

SDK build pipeline:
  1. SDK CI fetches https://api.cypherx.ai/v1/openapi.json (or pin a tag).
  2. Pin the OpenAPI commit SHA + release tag in `sdk-version.json` (in SDK repo).
  3. Generate types:
     Python:     datamodel-code-generator (Pydantic v2 output)
     TypeScript: openapi-typescript (compile-time types only) + zod-from-openapi (runtime guards, opt-in)
  4. Generated code goes to `src/generated/` (gitignored in source; rebuilt per release).
  5. Hand-written convenience layer in `src/client/` on top of generated types.
  6. On API release: SDK CI opens an auto-PR with the regenerated diff for human review
     (no auto-merge — type changes need a human eyeball).

SDK ↔ API version alignment (semver discipline):
  SDK MAJOR bumps when API introduces /v2/ and SDK migrates to it. Old SDK stays
                  on /v1/ (frozen — receives only security patches).
  SDK MINOR bumps when new endpoints are added (additive — caller code stays compatible).
  SDK PATCH bumps for SDK-only fixes (no API change).

  README publishes the version matrix:
    cypherx-ai 0.1.x  → API v1 (Phase 9A coverage)
    cypherx-ai 0.2.x  → API v1 (adds streaming, Memory, RAG)
    cypherx-ai 0.3.x  → API v1 (adds workflows)
    cypherx-ai 1.0.x  → API v1 (GA — surface frozen for 1 year)
    cypherx-ai 2.0.x  → API v2 (when /v2/ ships)
```

This ensures SDKs never drift from the actual API.

---

### Component 3b — Cross-Cutting SDK Behaviours ⚡ (applies to all SDK languages)

These behaviours are LANGUAGE-AGNOSTIC contracts that every SDK MUST implement identically. Drift between Python and TypeScript on any of these is a release blocker.

**Idempotency-Key auto-generation:**
```
Every mutation request (POST / PUT / PATCH / DELETE) MUST carry Idempotency-Key per
Contract 9. SDK auto-generates Idempotency-Key: <uuid4> for every mutation unless
caller passes idempotency_key= explicitly.

Caller can:
  - Override:  agent.submit_task(..., idempotency_key="my-key-123")
  - Read it:   task.idempotency_key  (held CLIENT-SIDE by the SDK from its own
                                       generation; NOT echoed by the server in the
                                       response body. SDK attaches it to the
                                       returned task object purely for caller
                                       log-correlation convenience.)

Why auto-generate:
  - The Idempotency-Key TTL is 24h per Contract 9.
  - SDK retries (per "Retry policy" below) reuse the SAME key on every attempt —
    so a retried POST /v1/tasks never creates a duplicate task.

NOT generated for GET / HEAD / OPTIONS (those are naturally idempotent).
```

**Typed exception hierarchy (mirrored across languages):**
```
CypherXError                            (base — carries request_id, trace_id, raw response)
├── CypherXAuthError                    (401 — invalid/expired JWT after one re-exchange)
├── CypherXForbiddenError               (403 — scope insufficient)
├── CypherXNotFoundError                (404 — resource not found)
├── CypherXConflictError                (409 — agent runtime not configured / dup)
├── CypherXValidationError              (422 VALIDATION_ERROR — carries field_path)
├── CypherXGuardrailError               (422 GUARDRAIL_VIOLATION — carries violations[])
├── CypherXRateLimitError               (429 — retry_after seconds accessible)
├── CypherXBudgetError                  (402 BUDGET_EXCEEDED)
├── CypherXQuotaError                   (429 QUOTA_EXCEEDED — distinct from rate limit)
├── CypherXTimeoutError                 (HTTP timeout on client side)
├── CypherXServerError                  (5xx — retryable per "Retry policy")
└── CypherXMultipleResultsError         (agent name resolution returned >1 UUID)

Every exception:
  .request_id      → from Contract 2 error body
  .trace_id        → for cross-service log correlation
  .code            → Contract 2 error code (string enum)
  .message         → safe-to-log message
  .details         → Contract 2 details payload (typed per error code)
  .response_body   → raw HTTP response (for debugging only; NOT for production code)
  .to_dict()       → serialisation for structured logs
```

**Retry policy (per HTTP method × status):**
```
Method    | 5xx     | 408 / 429        | 4xx (other)  | Connection err
----------|---------|------------------|--------------|----------------
GET       | retry 3 | honour Retry-After | NO retry   | retry 3
HEAD      | retry 3 | honour Retry-After | NO retry   | retry 3
OPTIONS   | retry 3 | honour Retry-After | NO retry   | retry 3
POST/PUT/ | retry 3 | honour Retry-After | NO retry   | retry 3
PATCH/    | (only if Idempotency-Key set — auto-generated by SDK)
DELETE    |         |                  |              |

Exponential backoff: 500ms × 2^n + jitter (0–250ms), capped at 30s.
429 with Retry-After: use the header value, NOT exponential backoff.
After max_retries (default 3): surface the typed exception above.
```

**Pagination iterator (every list endpoint):**
```
Python:
  for agent in client.agents.list():
      print(agent.name)
  # Iterator transparently fetches next_cursor pages per Contract 9.
  # client.agents.list(limit=20) controls page size; total never materialised.

TypeScript:
  for await (const agent of client.agents.list()) { console.log(agent.name) }

Lower-level (single page):
  page = client.agents.list_page(limit=20, cursor=None)
  page.items, page.next_cursor, page.has_more

Pagination defaults: limit=20; max=100 (server-enforced per Contract 9).
```

**Trace propagation (Contract 8):**
```
Every SDK HTTP call:
  - Generates a fresh `traceparent` (W3C trace context) if no caller-supplied one.
  - Accepts caller-supplied `traceparent` (for SDK-in-SDK chains):
      client.with_trace(traceparent="00-...-...-01").agents("X").submit_task(...)
  - Sets `tracestate: cypherx=sdk@<sdk-version>` on the FIRST outbound hop only.
  - Every response object exposes `.trace_id` (uppermost trace context).

cypherx-vendor tracestate value format (canonical across SDK + Phase 10 + Kong):

  tracestate: cypherx=<subkey1>@<value>;<subkey2>@<value>;...

  Subkeys defined across the platform:
    sdk     = "<sdk-version>"           (set by SDK on first outbound only)
    tenant  = "<tenant_id>"             (set by Auth/Kong after JWT verify)
    wf      = "<workflow_id>"           (set by Phase 10 a2a-router on A2A delegation)
    ptask   = "<parent_task_id>"        (set by Phase 10 on A2A delegation)

  Append semantics — services MUST append/replace specific subkeys, NEVER overwrite
  the entire cypherx= value. Example evolution of one trace:
    1. SDK sends:           tracestate: cypherx=sdk@0.1.5
    2. Kong appends tenant: tracestate: cypherx=sdk@0.1.5;tenant=<tenant_id>
    3. a2a-router appends:  tracestate: cypherx=sdk@0.1.5;tenant=<tenant_id>;wf=<uuid>;ptask=<uuid>

  Parsing rule (every consumer): split on ";", split each on "@", merge into a
  string→string map. Unknown subkeys ignored (forward-compat).

Customers correlate by logging task.trace_id in their app logs; the same trace_id
appears in Grafana Tempo (Phase 1) and in xagent.task_steps (Phase 9 post-edit).
```

**Telemetry:**
```
NO telemetry by default — no phone-home, no usage metrics, nothing.

If added in a future SDK release (📋), it would be:
  CypherXClient(telemetry=True)
  Payload: SDK version, language version, OS, anonymous hash of API-key prefix.
  No request bodies, no response bodies, no error messages with content.

For first release: do not add the parameter. Easier to add later than to remove a
default-on telemetry that breaks customer trust.
```

---

### Component 4 — Go SDK 📋 (P2)

```
Package: github.com/cypherx-ai/go-sdk
Target audience: infrastructure engineers

Same coverage as Python/TypeScript SDK.
Idiomatic Go: contexts, error handling, channels for streaming.
```

---

### Component 5 — SDK Documentation & Examples 📋

```
docs.cypherx.ai/sdk/
  ├── quickstart                      (Python + TypeScript node + TypeScript browser/BFF)
  ├── authentication                  (API key → JWT exchange, env-var pattern, browser BFF)
  ├── submitting-tasks
  ├── streaming                       (v0.2+ — flagged as future)
  ├── using-tools-and-skills          (read-only listing in v0.1; test_invoke in v0.2)
  ├── building-workflows              (v0.3+ — flagged as future)
  ├── error-handling                  (typed exceptions, retry policy)
  ├── pagination                      (iterator usage)
  └── trace-correlation               (logging task.trace_id, Tempo lookup)

GitHub repos: cypherx-ai/examples/
  ├── python/
  │   ├── quickstart/                 (SDK ≥ 0.1; single task)
  │   ├── research-agent/             (SDK ≥ 0.1; agent with tool-web-search@1.0.0)
  │   ├── rag-powered-qa/             (SDK ≥ 0.2; uses RAG + streaming)
  │   └── multi-agent-workflow/       (SDK ≥ 0.3; requires Phase 10)
  └── typescript/
      ├── node/                       (same examples as Python)
      └── browser-bff/                (Next.js example showing the BFF pattern + browser SDK)

Every example README pins:
  - Minimum SDK version
  - API version (v1 / v2)
  - Phase-coverage required (e.g., "requires Phase 10 stable in your tenant")
```

---

### Cross-phase resources owned by Phase 14

Phase 14 is mostly client-side code (SDK packages on PyPI/npm), but it needs a
small server-side footprint to support CI integration tests. Grouped under one
directory + one Secrets Manager entry — same pattern Phases 8/10/12/13 use.

**1. Migrations directory — `platform-migrations/phase-14/`:**

```
platform-migrations/phase-14/
  ├── 20261101_0900__sdk_integration_test_agent.sql    → auth.agents + auth.api_keys
  │     (well-known integration-test agent and a long-lived API key)
  └── README.md

Seeded rows:
  - agent_id = 00000000-0000-0000-0000-0000000000b1
    tenant_id = 00000000-0000-0000-0000-0000000000ff   (the integration-test tenant
                                                        from Contract 13 — accepted
                                                        ONLY in dev/staging, rejected
                                                        in prod)
    name = "sdk-ci-integration-test"
    scopes = standard test agent scopes (chat invoke, embed, etc. — no admin)
  - API key created via standard Phase 2 mechanism; raw value captured at seed time
    and written to Secrets Manager at the path below (this happens in the
    migration's post-script, not in SQL — the raw key cannot be reproduced from
    the hash stored in auth.api_keys).

Run-once at SDK-CI infrastructure bootstrap. CODEOWNERS = Auth team
(this writes to auth.* tables).
```

**2. Secrets Manager entry:**

```
Path:    cypherx/ci/sdk-integration-test/api_key
         (matches Phase 1 Component 18's `cypherx/ci/*` convention — GitHubActionsRole
          already has scoped `secretsmanager:GetSecretValue` on this prefix)
Format:  raw API key string (cx_dev_... for dev cluster integration tests; never
         a prod-cluster key — prod doesn't accept tenant ...ff anyway per
         Contract 13)
Rotation: yearly via runbook. Procedure:
          (a) Issue new API key via Phase 2 auth.api_keys mechanism for the same
              agent (the issue + revoke endpoints are unchanged).
          (b) Write new key to Secrets Manager (overwrites previous version; AWS
              keeps version history for rollback).
          (c) Revoke old key after one CI cycle confirms green.
```

**3. No new Terraform module needed** — uses Phase 1's GitHubActionsRole + IAM
   scope (Component 18 post-edit), Phase 2's auth.* tables, no new infra surface.

---

## 📋 Full Enterprise Implementation Checklist

**SDK v0.1 (first release — Phase 9A API coverage):**
- [ ] Python SDK architecture planned separately
- [ ] TypeScript SDK architecture planned separately
- [ ] **Auth flow internalised**: API key → JWT exchange (Contract 1 + Phase 2); api_key sent in REQUEST BODY of `POST /v1/agents/{id}/token` (no Authorization header on this one call); API key NEVER on the wire after the exchange; env var `CYPHERX_API_KEY` is the recommended pattern
- [ ] **Agent identifier — UUID ONLY** in v0.1: `client.agents(uuid)` and `client.agent_by_id(uuid)`; env vars `CYPHERX_API_KEY` + `CYPHERX_AGENT_ID`; name-based resolution dropped because it requires `tenant:admin` (unavailable on normal agent API keys). Future `GET /v1/agents/by-name/{name}` scoped to `agent:read` tracked 📋 in Phase 2.
- [ ] **TypeScript split**: `@cypherx-ai/sdk` (Node mode) + `@cypherx-ai/sdk/browser` (BFF-only, NO `apiKey` AND NO `jwt` parameter — both type-rejected)
- [ ] **Browser SDK auth via session cookie + CSRF only**: `credentials: 'include'` for session, `getCsrfToken` callback reads the non-httpOnly `csrf` cookie and double-submits per Phase 12; NO JWT ever touches the SPA
- [ ] **Customer-BFF Protocol (Component 2b)** documented and required for browser SDK: 8 v0.1 endpoints + 4 v0.2 additions; reference Next.js implementation ships in `examples/typescript/browser-bff/`; BFF forwards trace headers verbatim; maps Contract 2 errors verbatim
- [ ] **Cross-cutting behaviours** (Component 3b) implemented identically in every language:
      - Idempotency-Key auto-generation on every mutation; same key on every retry; `task.idempotency_key` returned from SDK's CLIENT-SIDE state (not echoed by server)
      - Typed exception hierarchy (`CypherXAuthError`, `CypherXGuardrailError`, etc.) carrying `request_id`, `trace_id`, `code`, `details`
      - Retry policy per method × status (GET retry 3; mutations retry only if Idempotency-Key set; honour Retry-After on 429)
      - Pagination iterator (transparently fetches `next_cursor` per Contract 9)
      - **Trace propagation with the canonical cypherx-vendor subkey format** — SDK sets `cypherx=sdk@<version>` on first outbound only; downstream services APPEND their own subkeys (`tenant`, `wf`, `ptask`) with `;` separators, never overwriting the entire cypherx= value; parsing rule documented
      - NO telemetry by default
- [ ] xAgent client: `create_agent` (two-step Auth + xAgent /runtime), `submit_task` (sync only), `get_task`, `cancel_task`, `tools.list_for_agent` (read-only)
- [ ] AuthClient: `register_agent`, `issue_key` (issue_token is SDK-internal — Auth model section)
- [ ] LLMsClient: `chat` (non-streaming), `embed`
- [ ] GuardrailsClient: `check_input`, `check_output` (with `input_text` per Phase 4 post-edit)
- [ ] Pydantic v2 models (Python) / TypeScript types from OpenAPI per Component 3 pipeline
- [ ] Constructor: env-var default; injectable `http_client` for corp proxy / custom CA
- [ ] Published to PyPI (`cypherx-ai`) + npm (`@cypherx-ai/sdk`)
- [ ] Changelog maintained per SDK version

**SDK v0.2 (lands AFTER Phase 9 streaming + Memory + RAG):**
- [ ] xAgent `stream_task` (async iterator over SSE events)
- [ ] LLMsClient `stream_chat` (async iterator)
- [ ] MemoryClient: `store`, `retrieve`
- [ ] RAGClient: `ingest`, `query`
- [ ] `tools.test_invoke` (platform:admin scope — direct tool invocation for testing)

**SDK v0.3 (lands AFTER Phase 10 + Phase 13 Domain 7 SPIFFE migration):**
- [ ] xAgent: `submit_workflow`, `get_workflow`, `cancel_workflow`

**Go SDK (P2):**
- [ ] Go SDK basic coverage (v0.1-equivalent) — idiomatic Go: contexts, error returns, channels for streaming when v0.2 lands

**Documentation & Examples:**
- [ ] SDK documentation on `docs.cypherx.ai/sdk/`
- [ ] Example projects: 4+ per language on GitHub; each README pins minimum SDK version + required Phase coverage
- [ ] TypeScript browser-bff example (Next.js) demonstrating the Phase 12 BFF pattern + browser SDK
- [ ] SDK ↔ API version matrix published in main SDK README

**CI & Release:**
- [ ] **`platform-migrations/phase-14/`** seeds the integration-test agent (`...00b1`) + API key into tenant `...ff` (dev/staging only); raw API key written to Secrets Manager at `cypherx/ci/sdk-integration-test/api_key` (matches Phase 1 Component 18 CI namespace — GitHubActionsRole already scoped); yearly rotation runbook
- [ ] SDK CI integration tests run against **staging cluster + Contract 13 integration-test tenant** (`00000000-0000-0000-0000-0000000000ff`); test API key fetched from `cypherx/ci/sdk-integration-test/api_key` via GitHub OIDC role; tenant data wiped nightly
- [ ] CI on every PR; release CI bumps version + publishes to PyPI / npm + tags GitHub release
- [ ] OpenAPI regeneration PR opened automatically on API release (no auto-merge; human review of diff)
- [ ] SDK release blocks if `prometheus-alerts-lint.yml` for SDK telemetry fails (when telemetry lands)

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. SDK Runtime Complexity — PARTIAL
Evidence: lines 28, 131–164, 367–387, 415–429. Auth refresh + idempotency + retry documented; signed-bundle validation listed but not implemented in v0.1.
**Mitigation:** SDK v0.1 MUST validate signed task bundles per Contract 6 before returning. Validation failures raise `CypherXValidationError`. JWKS rotation via Contract 1 — no SDK caching of verification keys.

### 2. Customer BFF Operational Overhead — REAL
Evidence: lines 261–323 (12 endpoints; ops cost downplayed).
**Mitigation:** document operational requirements — session/CSRF model MUST match Phase 12 exactly (use Phase 12 audit checklist at deploy); JWT cache per agent in BFF; preserve Contract 2 error codes verbatim; integrate with org observability (trace correlation, error tracking). Reference impl ships basic structured logging only.

### 3. OpenAPI Aggregation Fragility — REAL
Evidence: lines 329–357 (no collision detection, no drift handling).
**Mitigation:** platform-mgmt CI rejects aggregated spec on endpoint-path collision. Per-service spec versions pinned; breaking spec changes (endpoint removal, response-shape change) require manual review. SDK CI validates generated types compile before opening auto-PR. Per-service semver tracked 📋.

### 4. Generated vs Handwritten Layer Drift — REAL
Evidence: lines 326–343 (no sync mechanism).
**Mitigation:** CI runs strict type-check (mypy / TypeScript strict) on `src/client/` against regenerated `src/generated/`; compile failure blocks release. Hand-written method signatures must match generated request/response types (added params allowed; reordering/omitting required fields rejected). Auto-PR review checklist mandates documenting wrapper deviations.

### 5. Future Tool Exposure Constraints — REAL
Evidence: lines 49–50, 79–84 (read-only in v0.1, `test_invoke` gated v0.2; no roadmap).
**Mitigation:** policy — v0.1 read-only via `tools.list_for_agent()`; v0.2 admin `tools.test_invoke()`; direct named-tool invocation via SDK requires (a) tool registry versioning (Phase 13), (b) per-language SDK changes per new tool, (c) 12-month deprecation notice. No customer code should depend on direct tool invocation via SDK at v0.2.

### 6. API Compatibility Governance — REAL
Evidence: lines 345–357 (semver only; no breaking-change policy).
**Mitigation:** breaking changes require 6-month deprecation window — month 1: bulletin; month 3: deprecation header on affected endpoint; month 6: removal. SDK major bumps tie to API major bumps only; old SDK majors receive security patches on prior API version. Phase 2 owns the policy; SDK CI runs integration tests against all API versions in the policy window.

### 7. Workflow Orchestration Complexity Explosion — REAL
Evidence: lines 74–77 (v0.3 exposes submit/get/cancel; no subtask control).
**Mitigation:** v0.3 scope — submit + status-poll + cancel only. NO subtask-level control (no retry-single-subtask, no skip-to-alternate-branch). Workflow logic stays server-side (Phase 10). Customers needing per-subtask control must use the platform API directly with a custom orchestrator; SDK workflow surface is high-level automation only. Subtask streaming + per-subtask error callbacks tracked 📋 post-v0.3.
