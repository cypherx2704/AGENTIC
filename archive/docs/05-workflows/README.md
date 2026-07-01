# 05 · Workflows

## Workflow Index

| # | Workflow | Entry Point |
|---|---------|-------------|
| [5.1](#51-agent-registration) | Agent Registration | POST /v1/agents |
| [5.2](#52-api-key-issuance--login) | API Key Issuance & Login | POST /v1/agents/{id}/api-keys → /v1/agents/{id}/token |
| [5.3](#53-admin-console-login) | Admin Console Login | Browser → BFF → Auth |
| [5.4](#54-agent-task-execution-first-cycle-spine) | Agent Task Execution (Spine) | POST /v1/tasks |
| [5.5](#55-agent-task-execution-wp12-enhanced) | Agent Task Execution (WP12 Enhanced) | POST /v1/tasks + RAG/Memory/Tools |
| [5.6](#56-rag-knowledge-base-ingestion) | RAG Knowledge Base Ingestion | POST /v1/kbs + POST /v1/ingest |
| [5.7](#57-memory-retrieval--storage) | Memory Retrieval & Storage | POST /v1/memories/search + POST /v1/memories |
| [5.8](#58-token-revocation) | Token Revocation | DELETE /v1/tokens/{jti} |

---

## 5.1 Agent Registration

### Overview
An admin or API client registers a new agent under a tenant. This creates the agent record, issues an API key, and seeds the agent's initial configuration.

### Use Case Diagram
```mermaid
graph LR
    Admin["Platform Admin"]
    RegisterAgent["Register Agent"]
    IssueAPIKey["Issue API Key"]
    ConfigureScopes["Configure Scopes"]
    SetQuotas["Set Quotas"]
    Auth["auth-service"]

    Admin --> RegisterAgent
    Admin --> IssueAPIKey
    Admin --> ConfigureScopes
    Admin --> SetQuotas
    RegisterAgent --> Auth
    IssueAPIKey --> Auth
    ConfigureScopes --> Auth
    SetQuotas --> Auth
```

### Sequence Diagram
```mermaid
sequenceDiagram
    participant Admin as Admin / API Client
    participant Auth as auth-service

    Admin->>Auth: POST /v1/agents\n{name, description, scopes, config}
    Auth->>Auth: Validate request\nCreate agent record\nInsert audit_log
    Auth->>Auth: Emit cypherx.auth.agent.registered\n(outbox → Kafka)
    Auth-->>Admin: 201 {agent_id, tenant_id, status: active}

    Admin->>Auth: POST /v1/agents/{agent_id}/api-keys\n{name, scopes, expires_at}
    Auth->>Auth: Generate random key\nArgon2id hash + store\nAssociate with agent
    Auth-->>Admin: 201 {api_key_id, key: "cx_...", scopes}
    Note over Admin: Store key securely — it is shown ONCE
```

### Flow Steps
1. Admin sends `POST /v1/agents` with `Authorization: Bearer <platform_admin_jwt>`.
2. auth-service validates the JWT, confirms `platform:admin` scope.
3. Agent record is created with `status=active`.
4. An `audit_log` entry records the registration action.
5. `cypherx.auth.agent.registered` is written to the outbox and published to Kafka.
6. Admin issues API keys via `POST /v1/agents/{id}/api-keys`.
7. The raw API key is returned **once** (Argon2id hash stored; original never retrievable).

### Failure Paths
- `403 FORBIDDEN` — caller JWT lacks `platform:admin` scope.
- `409 CONFLICT` — agent name already exists in the tenant.
- `429 QUOTA_EXCEEDED` — tenant has reached max agent count.

---

## 5.2 API Key Issuance & Login

### Sequence Diagram
```mermaid
sequenceDiagram
    participant Client as API Client
    participant Auth as auth-service
    participant Valkey as Valkey

    Client->>Auth: POST /v1/agents/{agent_id}/token\n{api_key: "cx_..."}
    Auth->>Auth: Argon2id verify api_key against hash
    Auth->>Auth: Check agent status (active?)
    Auth->>Auth: Check quota (tokens/month)
    Auth->>Auth: Load signing key from signing_keys table
    Auth->>Auth: Mint RS256 JWT\n{sub, tenant_id, agent_id, scopes, exp}
    Auth->>Valkey: Cache JTI for revocation check
    Auth-->>Client: 200 {access_token: JWT, expires_in: 3600}
```

### Notes
- The JWT is valid for ≤3600s (configurable per plan).
- Clients should cache the JWT and re-mint only when it nears expiry.
- API key exchange is rate-limited per agent to prevent brute-force.

---

## 5.3 Admin Console Login

### Overview
The browser-based admin console uses the BFF as a security boundary. The SPA never holds tokens.

### Sequence Diagram
```mermaid
sequenceDiagram
    participant Browser as Browser (SPA)
    participant Edge as Edge (Caddy)
    participant BFF as frontend-bff
    participant Auth as auth-service
    participant Valkey as Valkey

    Browser->>Edge: GET / (SPA load)
    Edge-->>Browser: HTML + JS bundle

    Browser->>BFF: POST /bff/login\n{agent_id, api_key}\n(no token yet — permit-all route)
    BFF->>Auth: POST /v1/agents/{agent_id}/token\n{api_key}
    Auth-->>BFF: {access_token: JWT}
    BFF->>Valkey: AES-256-GCM encrypt session\nStore {jwt, tenant_id, csrf_token}\nKey: session_id UUID
    BFF-->>Browser: Set-Cookie: session=<session_id>; HttpOnly; Secure; SameSite=Lax\nSet-Cookie: cypherx_csrf=<csrf_token>; SameSite=Lax\n200 {tenant_id, agent_id}

    Browser->>BFF: GET /bff/me\n(Cookie: session=<session_id>)
    BFF->>Valkey: Decrypt session → {jwt, tenant_id}
    BFF-->>Browser: 200 {agent_id, tenant_id, scopes, plan}
```

### CSRF Flow
```mermaid
sequenceDiagram
    participant Browser as Browser
    participant BFF as frontend-bff

    Browser->>BFF: POST /bff/api/tasks\nX-CSRF-Token: <value from cypherx_csrf cookie>\nCookie: session=...; cypherx_csrf=<value>
    BFF->>BFF: Assert X-CSRF-Token header == cypherx_csrf cookie == session.csrfToken
    Note over BFF: All three must match — double-submit + session binding
    BFF->>BFF: Decrypt session → JWT
    BFF->>BFF: Inject Bearer JWT, X-Tenant-ID, traceparent
    BFF->>BFF: Strip all client-supplied Authorization / X-Tenant-ID headers
    BFF->>xAgent: POST /v1/tasks (proxied with injected headers)
```

---

## 5.4 Agent Task Execution (First-Cycle Spine)

### Overview
The minimum viable task flow: JWT verification → guardrails → LLM → guardrails → response.

### Use Case Diagram
```mermaid
graph LR
    Agent["Agent / Client"]
    SubmitTask["Submit Task"]
    CheckInput["Check Input Guardrails"]
    InvokeModel["Invoke LLM Model"]
    CheckOutput["Check Output Guardrails"]
    GetResult["Get Task Result"]
    Auth["auth-service"]
    GR["guardrails-service"]
    LLM["llms-gateway"]
    xA["xAgent/ax-1"]

    Agent --> SubmitTask
    Agent --> GetResult
    SubmitTask --> xA
    xA --> Auth
    xA --> CheckInput
    xA --> InvokeModel
    xA --> CheckOutput
    CheckInput --> GR
    InvokeModel --> LLM
    CheckOutput --> GR
```

### Sequence Diagram
```mermaid
sequenceDiagram
    participant Client as Client / BFF
    participant xA as xAgent/ax-1
    participant Auth as auth-service
    participant GR as guardrails-service
    participant LLM as llms-gateway
    participant DB as Neon (Postgres)
    participant Kafka as Redpanda

    Client->>xA: POST /v1/tasks\n{agent_id, input_text, metadata}\nAuthorization: Bearer JWT

    xA->>Auth: GET /.well-known/jwks.json (cached 24h)
    xA->>xA: Verify RS256 signature + exp + revocation
    xA->>DB: SET LOCAL app.tenant_id = '{tenant_id}'
    xA->>DB: INSERT tasks (status=processing)

    Note over xA: Stage: LOAD
    xA->>DB: SELECT agent config

    Note over xA: Stage: PRE_GUARDRAIL
    xA->>GR: POST /v1/check/input\n{text, task_id}\nsvc JWT + X-Forwarded-Agent-JWT
    GR->>DB: SET LOCAL app.tenant_id = '{tenant_id}'
    GR->>GR: Evaluate built-in + tenant rules
    GR->>DB: INSERT violations (if warn/block)
    GR-->>xA: {decision: allow, check_id}

    Note over xA: Stage: PROMPT_BUILD
    xA->>xA: Assemble system message + user message

    Note over xA: Stage: LLM
    xA->>LLM: POST /v1/chat/completions\n{model, messages}\nsvc JWT + X-Forwarded-Agent-JWT + X-Request-ID
    LLM->>LLM: Resolve model alias → provider
    LLM->>LLM: Call Anthropic/OpenAI
    LLM->>DB: INSERT usage_records (llm_call_id)
    LLM->>DB: INSERT outbox (request.completed event)
    LLM-->>xA: {choices[{message{content}}], usage}

    Note over xA: Stage: POST_GUARDRAIL
    xA->>GR: POST /v1/check/output\n{text: response, input_text, task_id}
    GR-->>xA: {decision: allow, check_id}

    Note over xA: Stage: EVENT
    xA->>DB: UPDATE tasks (status=completed, response_text, cost_usd)
    xA->>DB: INSERT task_steps (all stages)
    xA->>DB: INSERT outbox (task.completed event)
    DB->>Kafka: Outbox relay publishes events

    xA-->>Client: 200 A2A response\n{task_id, status, response, steps[], cost_usd}
```

### Success Path
1. `201` on `POST /v1/tasks` → `task_id` returned immediately if async, or full response if sync.
2. Task visible via `GET /v1/tasks/{task_id}`.
3. Two Kafka events emitted: `cypherx.llms.request.completed` + `cypherx.agent.task.completed`.

### Failure Paths
| Error | HTTP | Code | Cause |
|-------|------|------|-------|
| Invalid JWT | 401 | `UNAUTHORIZED` | Expired, bad signature, revoked |
| Quota exceeded | 429 | `QUOTA_EXCEEDED` | Tenant hit token or request limit |
| Input guardrail block | 422 | `GUARDRAIL_VIOLATION` | Pre-guardrail returns block |
| Output guardrail block | 422 | `GUARDRAIL_VIOLATION` | Post-guardrail returns block |
| LLM provider error | 502 | `PROVIDER_ERROR` | Provider 5xx or timeout |
| Idempotency conflict | 409 | `IDEMPOTENCY_CONFLICT` | Same key within 24h with different body |

---

## 5.5 Agent Task Execution (WP12 Enhanced)

WP12 stages are built into xAgent but disabled by default (`STAGE_ENABLE_RAG_QUERY`, `STAGE_ENABLE_MEMORY_RETRIEVE`, `STAGE_ENABLE_TOOL_LOOP`, `STAGE_ENABLE_MEMORY_WRITE`).

### Sequence Diagram (with all WP12 stages enabled)
```mermaid
sequenceDiagram
    participant xA as xAgent/ax-1
    participant GR as guardrails-service
    participant RAG as rag-service
    participant Mem as memory-service
    participant LLM as llms-gateway
    participant TR as tool-registry
    participant TW as tool-web-search

    Note over xA: Stage: PRE_GUARDRAIL
    xA->>GR: POST /v1/check/input

    Note over xA: Stage: RAG_QUERY (WP12)
    xA->>RAG: POST /v1/kbs/{kb_id}/query\n{query_text, top_k: 5}
    RAG-->>xA: {results[{chunk, score}]}

    Note over xA: Stage: MEMORY_RETRIEVE (WP12)
    xA->>Mem: POST /v1/memories/search\n{query_text, agent_id, top_k: 10}
    Mem-->>xA: {memories[{content, importance}]}

    Note over xA: Stage: PROMPT_BUILD (enriched)
    xA->>xA: Inject RAG context + memory history into prompt

    Note over xA: Stage: LLM
    xA->>LLM: POST /v1/chat/completions\n(with tools[{function_call definitions}])
    LLM-->>xA: {choices[{message{tool_calls: [{name, arguments}]}}]}

    Note over xA: Stage: TOOL_LOOP (WP12)
    loop for each tool_call
        xA->>TR: GET /v1/tools/{tool_name}
        TR-->>xA: {endpoint_url, version}
        xA->>TW: POST /mcp/v1/invoke\n{tool: "web_search", params: {query}}
        TW-->>xA: {result: {hits[]}}
    end
    xA->>LLM: POST /v1/chat/completions\n(with tool_results in messages)
    LLM-->>xA: {choices[{message{content: final_answer}}]}

    Note over xA: Stage: POST_GUARDRAIL
    xA->>GR: POST /v1/check/output

    Note over xA: Stage: MEMORY_WRITE (WP12)
    xA->>Mem: POST /v1/memories\n{content: response_summary, agent_id, session_id}
    Mem-->>xA: {memory_id}

    Note over xA: Stage: EVENT
    xA->>xA: Persist task + emit events
```

---

## 5.6 RAG Knowledge Base Ingestion

### Sequence Diagram
```mermaid
sequenceDiagram
    participant Client as API Client
    participant RAG as rag-service
    participant LLM as llms-gateway
    participant MinIO as MinIO (S3)
    participant Kafka as Redpanda

    Client->>RAG: POST /v1/kbs\n{name, embed_model}
    RAG-->>Client: 201 {kb_id}

    Client->>RAG: POST /v1/ingest\n{kb_id, title, content_inline: "..."}
    RAG->>RAG: Create document record (status=pending)
    RAG->>RAG: Write ingestion.requested to outbox
    RAG->>Kafka: Publish cypherx.rag.ingestion.requested

    Note over RAG: Ingest Worker (async)
    Kafka->>RAG: Consume ingestion.requested {doc_id}
    RAG->>RAG: Chunk document
    loop per chunk
        RAG->>LLM: POST /v1/embeddings\n{input: chunk_text}
        LLM-->>RAG: {embedding: [1536 floats]}
        RAG->>RAG: Insert chunk + chunk_vectors_1536
    end
    RAG->>MinIO: PUT /kbs/{kb_id}/docs/{doc_id}
    RAG->>RAG: UPDATE document status=indexed
    RAG->>Kafka: Publish cypherx.rag.ingestion.completed

    Client->>RAG: POST /v1/kbs/{kb_id}/query\n{query_text, top_k: 5}
    RAG->>LLM: POST /v1/embeddings {input: query_text}
    LLM-->>RAG: {embedding}
    RAG->>RAG: pgvector cosine similarity search on chunk_vectors_1536
    RAG-->>Client: 200 {results[{chunk, score, doc_id}]}
```

---

## 5.7 Memory Retrieval & Storage

### Sequence Diagram
```mermaid
sequenceDiagram
    participant xA as xAgent/ax-1
    participant Mem as memory-service
    participant LLM as llms-gateway

    Note over xA: Before LLM call — retrieve context
    xA->>Mem: POST /v1/memories/search\n{query: task_input, agent_id, top_k: 10}
    Mem->>LLM: POST /v1/embeddings {input: query}
    LLM-->>Mem: {embedding}
    Mem->>Mem: pgvector cosine search on memory_vectors_1536
    Mem-->>xA: {memories[{content, importance, created_at}]}

    Note over xA: After LLM response — store new memory
    xA->>Mem: POST /v1/memories\n{content: response_summary, agent_id, session_id, importance: 0.8}
    Mem->>LLM: POST /v1/embeddings {input: content}
    LLM-->>Mem: {embedding}
    Mem->>Mem: INSERT memories + memory_vectors_1536
    Mem->>Mem: INSERT outbox (memory.stored event)
    Mem-->>xA: 201 {memory_id}
```

---

## 5.8 Token Revocation

### Sequence Diagram
```mermaid
sequenceDiagram
    participant Admin as Admin
    participant Auth as auth-service
    participant Valkey as Valkey
    participant Kafka as Redpanda
    participant Services as All Services (xAgent, LLMs, GR, ...)

    Admin->>Auth: DELETE /v1/tokens/{jti}\n(or DELETE /v1/agents/{id}/tokens — revoke all)
    Auth->>Auth: INSERT revoked_tokens {jti, reason, revoked_at}
    Auth->>Auth: INSERT audit_log {action: token_revoked}
    Auth->>Auth: INSERT outbox (token.revoked event)
    Auth->>Valkey: SET cypherx:rev:jti:{jti} 1 EX 86400
    Auth->>Kafka: Publish cypherx.auth.token.revoked {jti, agent_id, tenant_id}
    Auth-->>Admin: 204 No Content

    Note over Services: On next request with this JWT
    Services->>Valkey: GET cypherx:rev:jti:{jti}
    Valkey-->>Services: 1 (found)
    Services-->>Client: 401 UNAUTHORIZED {code: TOKEN_REVOKED}

    Note over Services: On Valkey outage — FAIL OPEN
    Services->>Valkey: GET cypherx:rev:jti:{jti}
    Valkey-->>Services: (timeout / connection error)
    Services->>Services: Log warning: revocation_check_failed
    Services->>Services: Accept token (availability wins)
```
