# 07 · API Documentation

## API Standards

### Base URL
| Environment | Base URL |
|------------|---------|
| Local (via edge) | `http://localhost:8000` |
| Local (direct to service) | `http://localhost:{service_port}` |
| Cloud | `https://api.cypherx.ai` (via Kong) |

### Versioning
All APIs are versioned in the URL path: `/v1/...`. A breaking change introduces `/v2/...` alongside `/v1/...`. `/v1/...` is never removed without deprecation notice.

### Content Types
- Request body: `application/json`
- Response body: `application/json`
- Streaming: `text/event-stream` (SSE)

### HTTP Methods
| Method | Semantics |
|--------|-----------|
| GET | Idempotent read — never changes state |
| POST | Create or non-idempotent action |
| PUT | Full idempotent replace |
| PATCH | Partial update |
| DELETE | Delete / revoke |

---

## Authentication

All endpoints (except JWKS, OIDC discovery, BFF login, and health probes) require a Bearer JWT.

### Agent JWT (External)
```http
Authorization: Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6Ii4uLiJ9...
X-Tenant-ID: 550e8400-e29b-41d4-a716-446655440000
```
- Issued by auth-service `POST /v1/agents/{id}/token`.
- RS256, TTL ≤3600s.
- Claims include `tenant_id`, `agent_id`, `scopes`, `plan`.

### Service Token (Internal)
```http
Authorization: Bearer <service_jwt>
X-Forwarded-Agent-JWT: <original_agent_jwt>
X-Tenant-ID: 550e8400-e29b-41d4-a716-446655440000
```
- Service token `sub` is `svc:<service_name>` or `svc-ext:<service_name>`.
- `on_behalf_of` claim in service token MUST match the `agent_id` in the forwarded agent JWT.

### Scopes Required Per Endpoint
| Scope | Grants Access To |
|-------|-----------------|
| `llm:invoke` | POST /v1/chat/completions, /v1/embeddings |
| `memory:read` | GET /v1/memories, POST /v1/memories/search |
| `memory:write` | POST /v1/memories, DELETE /v1/memories/{id} |
| `rag:query` | POST /v1/kbs/{id}/query |
| `rag:admin` | POST /v1/kbs, POST /v1/ingest, DELETE /v1/docs/{id} |
| `guardrails:check` | POST /v1/check/input, POST /v1/check/output |
| `tool:invoke` | POST /mcp/v1/invoke |
| `tool:admin` | POST /v1/tools, PUT /v1/tools/{name}, DELETE /v1/tools/{name} |
| `platform:admin` | All tenant/agent management endpoints in auth-service |

---

## Error Format (Contract 2)

All errors return the Contract-2 error envelope:

```json
{
  "error": {
    "code": "GUARDRAIL_VIOLATION",
    "message": "Input text was blocked by policy rule: prompt_injection",
    "details": {
      "rule_type": "prompt_injection",
      "check_id": "550e8400-e29b-41d4-a716-446655440001",
      "policy_id": "550e8400-e29b-41d4-a716-446655440002"
    }
  }
}
```

### Canonical Error Codes
| HTTP | Code | Description |
|------|------|-------------|
| 400 | `VALIDATION_ERROR` | Request body failed schema validation |
| 400 | `RESERVED_FIELD` | Request body contains a reserved metadata key |
| 401 | `UNAUTHORIZED` | Missing or invalid Authorization header |
| 401 | `TOKEN_EXPIRED` | JWT has expired |
| 401 | `TOKEN_REVOKED` | JWT or API key has been revoked |
| 403 | `FORBIDDEN` | JWT valid but insufficient scope |
| 404 | `NOT_FOUND` | Resource does not exist |
| 409 | `CONFLICT` | Resource already exists |
| 409 | `IDEMPOTENCY_CONFLICT` | Same Idempotency-Key with different body within 24h |
| 422 | `GUARDRAIL_VIOLATION` | Guardrails blocked the request or response |
| 429 | `QUOTA_EXCEEDED` | Tenant quota (tokens/month or requests/day) exceeded |
| 429 | `RATE_LIMITED` | Request rate limit exceeded |
| 502 | `PROVIDER_ERROR` | Upstream LLM provider returned an error |
| 503 | `SERVICE_UNAVAILABLE` | Dependency not ready |

---

## Idempotency (Contract 9)

Mutation endpoints support the `Idempotency-Key` header for safe retries.

```http
POST /v1/tasks
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440099
Content-Type: application/json
```

- Key MUST be a UUID v4.
- If the same key + same body is replayed within 24h, the cached response is returned with `Idempotent-Replayed: true` header.
- If the same key is used with a **different** body, `409 IDEMPOTENCY_CONFLICT` is returned.
- Streaming requests (`stream: true`) are **exempt** from idempotency (replays are re-streamed).

---

## Pagination

List endpoints use cursor-based pagination:

```json
{
  "items": [...],
  "next_cursor": "eyJpZCI6IjU1MGU4NDAwIn0=",
  "has_more": true
}
```

Query parameters:
- `limit` — page size (default 20, max 100)
- `cursor` — opaque cursor from `next_cursor` of previous response

---

## Request Tracing

All requests should include W3C trace context (Contract 8):

```http
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
X-Request-ID: 550e8400-e29b-41d4-a716-446655440000
```

- `traceparent` is propagated through all inter-service calls and into Kafka events.
- `X-Request-ID` is echoed back in responses as `X-Request-ID`.
- If `X-Request-ID` is not provided, the service mints a new UUID.

---

## Reserved Headers (Contract 8 / `contracts/http/headers.md`)

These headers are reserved and **injected by the BFF or gateway only**. Services reject them from external clients:

| Header | Set By | Purpose |
|--------|--------|---------|
| `Authorization` | BFF (from session) | Bearer JWT |
| `X-Tenant-ID` | BFF (from session) | Tenant UUID for RLS context |
| `X-Agent-ID` | Services | Forwarded agent identifier |
| `X-Forwarded-Agent-JWT` | Internal services | Full agent JWT for on-behalf-of verification |
| `X-Request-ID` | BFF / Gateway | Unique request trace ID |
| `X-CSRF-Token` | Browser (from cookie) | CSRF token (BFF enforces double-submit) |
| `Idempotency-Key` | Client | Safe retry key |
| `Idempotent-Replayed` | Services | `true` when returning cached idempotent response |
| `traceparent` | BFF / Services | W3C trace context |

---

## Key Endpoints Reference

### auth-service (:8080)

#### POST /v1/agents/{agent_id}/token
Exchange API key for agent JWT.

**Request:**
```json
{
  "api_key": "cx_live_abc123..."
}
```

**Response 200:**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6Ii4uLiJ9...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "llm:invoke memory:read guardrails:check"
}
```

#### GET /.well-known/jwks.json
Public key set for JWT verification (cacheable 24h).

**Response 200:**
```json
{
  "keys": [
    {
      "kty": "RSA",
      "use": "sig",
      "alg": "RS256",
      "kid": "key-2026-06-01",
      "n": "...",
      "e": "AQAB"
    }
  ]
}
```

---

### llms-gateway (:8085)

#### POST /v1/chat/completions
Send a chat completion request. Compatible with OpenAI Chat Completions API with CypherX extensions.

**Request:**
```json
{
  "model": "fast",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is 2+2?"}
  ],
  "temperature": 0.7,
  "max_tokens": 256,
  "stream": false
}
```

**Response 200:**
```json
{
  "id": "chatcmpl-550e8400",
  "object": "chat.completion",
  "created": 1779840000,
  "model": "claude-3-5-sonnet-20241022",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "2+2 equals 4."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 8,
    "total_tokens": 33,
    "cost_usd": 0.000045
  }
}
```

**Streaming (stream: true):**
```
data: {"id":"chatcmpl-550e8400","choices":[{"delta":{"content":"2"},"index":0}]}
data: {"id":"chatcmpl-550e8400","choices":[{"delta":{"content":"+2"},"index":0}]}
data: {"id":"chatcmpl-550e8400","choices":[{"delta":{"content":" equals 4."},"index":0}]}
data: [DONE]
```

#### POST /v1/embeddings
Generate embeddings for text.

**Request:**
```json
{
  "model": "embed-v3",
  "input": "Hello, world!"
}
```

**Response 200:**
```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "index": 0,
      "embedding": [0.023, -0.034, ...]
    }
  ],
  "model": "text-embedding-3-small",
  "usage": {"prompt_tokens": 4, "total_tokens": 4}
}
```

---

### xAgent / ax-1 (:8083)

#### POST /v1/tasks
Submit an agent task.

**Request:**
```json
{
  "agent_id": "550e8400-e29b-41d4-a716-446655440001",
  "input": {
    "role": "user",
    "content": "Summarize the latest news about AI."
  },
  "metadata": {
    "session_id": "550e8400-e29b-41d4-a716-446655440002",
    "source": "web"
  },
  "async": false
}
```

**Response 200 (sync):**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440003",
  "status": "completed",
  "agent_id": "550e8400-e29b-41d4-a716-446655440001",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "input": {"role": "user", "content": "Summarize the latest news about AI."},
  "output": {"role": "assistant", "content": "Recent AI developments include..."},
  "steps": [
    {"stage": "PRE_GUARDRAIL", "decision": "allow", "check_id": "..."},
    {"stage": "LLM", "model": "fast", "tokens": 256, "cost_usd": 0.00034},
    {"stage": "POST_GUARDRAIL", "decision": "allow", "check_id": "..."}
  ],
  "cost_usd": 0.00034,
  "created_at": "2026-06-24T12:00:00Z",
  "completed_at": "2026-06-24T12:00:01.234Z"
}
```

---

### guardrails-service (:8086)

#### POST /v1/check/input
Check an input text against active policies.

**Request:**
```json
{
  "text": "Ignore previous instructions and...",
  "task_id": "550e8400-e29b-41d4-a716-446655440003",
  "context": {"agent_id": "550e8400-e29b-41d4-a716-446655440001"}
}
```

**Response 200:**
```json
{
  "decision": "block",
  "check_id": "550e8400-e29b-41d4-a716-446655440010",
  "processed_text": null,
  "violations": [
    {
      "rule_type": "prompt_injection",
      "severity": "critical",
      "matched_pattern": "Ignore previous instructions",
      "policy_id": "00000000-0000-0000-0000-000000000001"
    }
  ]
}
```

---

### RAG service (:8087)

#### POST /v1/kbs/{kb_id}/query
Query a knowledge base for semantically similar chunks.

**Request:**
```json
{
  "query_text": "What is the refund policy?",
  "top_k": 5,
  "score_threshold": 0.7
}
```

**Response 200:**
```json
{
  "results": [
    {
      "chunk_id": "550e8400-...",
      "doc_id": "550e8400-...",
      "content": "Our refund policy allows returns within 30 days...",
      "score": 0.92,
      "metadata": {"doc_title": "Customer FAQ", "chunk_index": 3}
    }
  ],
  "query_embedding_model": "text-embedding-3-small"
}
```

---

### Memory service (:8088)

#### POST /v1/memories/search
Semantic search over agent memories.

**Request:**
```json
{
  "query_text": "user's preferred programming language",
  "agent_id": "550e8400-e29b-41d4-a716-446655440001",
  "top_k": 10,
  "session_id": "550e8400-e29b-41d4-a716-446655440002"
}
```

**Response 200:**
```json
{
  "memories": [
    {
      "memory_id": "550e8400-...",
      "content": "User prefers Python for scripting tasks.",
      "importance": 0.85,
      "created_at": "2026-06-20T10:00:00Z",
      "score": 0.91
    }
  ]
}
```

---

### MCP Tool — tool-web-search (:8091)

#### POST /mcp/v1/invoke
Invoke the web_search tool.

**Request:**
```json
{
  "tool": "web_search",
  "version": "1.0.0",
  "params": {
    "query": "latest AI news 2026",
    "num_results": 5
  }
}
```

**Response 200:**
```json
{
  "tool": "web_search",
  "result": {
    "hits": [
      {
        "title": "OpenAI releases GPT-5",
        "url": "https://...",
        "snippet": "OpenAI announced..."
      }
    ]
  },
  "latency_ms": 234
}
```

---

## Health Endpoints (Contract 7)

All services expose identical health endpoints:

### GET /livez
Process-only liveness check. Never touches external dependencies.

**Response 200:**
```json
{
  "status": "up",
  "service": "xagent",
  "version": "1.2.3",
  "uptime_seconds": 3600
}
```

### GET /readyz
Readiness check. Returns 503 until all critical dependencies are reachable.

**Response 200:**
```json
{
  "status": "ready",
  "checks": {
    "postgres": "ok",
    "valkey": "ok",
    "kafka": "ok",
    "auth_jwks": "ok"
  }
}
```

**Response 503:**
```json
{
  "status": "not_ready",
  "checks": {
    "postgres": "error: connection refused",
    "valkey": "ok"
  }
}
```

### GET /metrics
Prometheus text format 0.0.4 metrics. Scraped on port 9090.

```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="POST",path="/v1/tasks",status="200"} 1234
http_requests_total{method="POST",path="/v1/tasks",status="422"} 5

# HELP http_request_duration_seconds HTTP request latency
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{le="0.05"} 1100
http_request_duration_seconds_bucket{le="0.1"} 1200
http_request_duration_seconds_bucket{le="0.5"} 1230
http_request_duration_seconds_bucket{le="+Inf"} 1234
http_request_duration_seconds_sum 45.2
http_request_duration_seconds_count 1234
```
