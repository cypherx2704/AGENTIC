# 02 · Requirements

## Functional Requirements

### FR-01 · Agent Identity & Access Management
- The system MUST allow agents to be registered per-tenant with a unique identity.
- The system MUST issue RS256-signed JWTs (Contract 1) with tenant_id, agent_id, scopes, and plan.
- The system MUST expose a JWKS endpoint (`/.well-known/jwks.json`) for public-key distribution.
- The system MUST support OAuth2 `client_credentials` flow for machine-to-machine token exchange.
- The system MUST support API keys as an alternative authentication credential; API keys map to agent JWTs at issuance time.
- The system MUST support immediate token revocation; revocation MUST propagate to all services within one Kafka lag window.
- The system MUST rotate signing keys on a schedule (≤90-day key lifetime); old keys remain in JWKS for 24h after rotation.
- The system MUST enforce per-tenant quotas (tokens/month, requests/day) and return `QUOTA_EXCEEDED` (429) when breached.

### FR-02 · Unified LLM Gateway
- The system MUST expose a single normalized `POST /v1/chat/completions` endpoint compatible with the OpenAI schema superset.
- The gateway MUST normalize requests to both Anthropic and OpenAI providers without the caller knowing which provider is used.
- The gateway MUST meter every token consumed (prompt + completion + cache tokens) and associate cost in USD per call.
- The gateway MUST support streaming (`stream: true`) via Server-Sent Events.
- The gateway MUST support BYOK: if a tenant has a provider key registered, it MUST be used in preference to the platform key.
- The gateway MUST support embeddings (`POST /v1/embeddings`) and reranking (`POST /v1/rerank`).
- The gateway MUST emit a `cypherx.llms.request.completed` Kafka event atomically with each billing record write.

### FR-03 · Guardrails (Safety Filter)
- The system MUST evaluate every agent task input through the guardrails service before LLM inference.
- The system MUST evaluate every LLM output through the guardrails service before returning it to the caller.
- Guardrails MUST support decisions: `allow`, `warn`, `redact`, and `block`.
- On `block`, xAgent MUST return `422 GUARDRAIL_VIOLATION` without invoking the LLM.
- Guardrails MUST support at least 11 built-in rule types (prompt injection, PII, hate speech, etc.).
- Guardrails MUST support tenant-defined custom policies with arbitrary rule chains.
- Guardrails MUST support HMAC-keyed redaction: detected PII patterns replaced with deterministic tokens.
- Guardrails MUST record every violation to the `guardrails.violations` table for audit.

### FR-04 · Agent Task Runtime
- The system MUST accept agent tasks via `POST /v1/tasks` (Contract 3 A2A format).
- xAgent MUST verify the caller's JWT locally against the auth JWKS on every request.
- xAgent MUST execute the named stage pipeline: `LOAD → PRE_GUARDRAIL → PROMPT_BUILD → LLM → POST_GUARDRAIL → EVENT`.
- The system MUST support task cancellation (`DELETE /v1/tasks/{id}`).
- The system MUST support task status polling (`GET /v1/tasks/{id}`).
- The system MUST support streaming task responses via SSE (`GET /v1/tasks/{id}/stream`).
- xAgent MUST emit a `cypherx.agent.task.completed` or `.failed` Kafka event atomically with the task result write.
- All task mutations MUST be idempotent when `Idempotency-Key` header is supplied (24h window).

### FR-05 · RAG Knowledge Bases
- The system MUST allow tenants to create named knowledge bases.
- The system MUST support document ingestion via inline content, presigned S3 URL, or Kafka work-order.
- The system MUST chunk and embed documents using the llms-gateway embeddings endpoint.
- The system MUST support semantic similarity search using pgvector.
- The system MUST enforce per-KB access control lists (KB ACLs).

### FR-06 · Agent Memory
- The system MUST store agent-generated memories scoped to a principal (agent + tenant).
- The system MUST support semantic memory search via pgvector.
- The system MUST support session-based memory grouping.
- The system MUST support GDPR bulk-wipe: delete all memories for a principal on request.
- All memory operations MUST be isolated by tenant via RLS.

### FR-07 · Tool Registry & MCP Tools
- The system MUST provide a central catalogue of available MCP tools.
- Tool discovery MUST support filtering by name, version, and capability.
- The registry MUST health-poll registered tool servers.
- MCP tool servers MUST expose `GET /manifest` and `POST /mcp/v1/invoke`.
- xAgent MUST be able to invoke tools via the registry without knowing the tool server's host.

### FR-08 · Multi-Tenancy
- Every tenant-scoped table MUST have Postgres RLS enforced via `app.tenant_id` set per transaction.
- The platform tenant (`00000000-0000-0000-0000-000000000001`) MUST be readable by services but not writable without `platform:admin` scope.
- Services MUST run as non-superuser, non-BYPASSRLS roles; cross-tenant access MUST be architecturally impossible.

### FR-09 · Observability
- Every service MUST expose `/livez`, `/readyz`, and `/metrics` (Contract 7).
- Every inter-service HTTP call and Kafka event MUST propagate W3C `traceparent` (Contract 8).
- All logs MUST be structured JSON (Contract 6) with at minimum: `timestamp`, `service`, `level`, `trace_id`, `tenant_id`, `message`.

### FR-10 · Frontend Admin Console
- The SPA MUST allow agents to be registered, configured, and monitored per tenant.
- The BFF MUST hold all session state (encrypted in Valkey); the SPA MUST never hold a token.
- The BFF MUST enforce CSRF (double-submit + session binding).
- The BFF MUST be the only component that proxies calls to backend services; the SPA MUST only talk to the BFF.

---

## Non-Functional Requirements

### NFR-01 · Performance

| SLO | Target | Notes |
|-----|--------|-------|
| Guardrails input check p99 | ≤ 50 ms | p50 ≤ 30 ms; `CLASSIFIER_MODE=stub` achieves this; detoxify may need tuning |
| Guardrails output check p99 | ≤ 100 ms | p50 ≤ 60 ms |
| Auth JWT issuance p99 | ≤ 200 ms | Argon2id hashing is the bottleneck for password flows |
| LLM gateway overhead p99 | ≤ 50 ms | Excluding provider latency |
| Task submission (sync) p99 | ≤ 500 ms | Excluding LLM provider latency |
| JWKS cache TTL | 24 h | Reduces auth-service load; stale after key rotation for ≤24h |
| Neon cold-start | ≤ 3 s | Services retry with `/readyz` 503 on cold start |

### NFR-02 · Security

- All JWTs MUST be RS256; HS256 is forbidden at the gateway and in all services.
- Signing private keys MUST live only in the encrypted `auth.signing_keys` DB table; never as env vars.
- Session payloads MUST be AES-256-GCM encrypted at rest in Valkey.
- All DSNs MUST include `sslmode=require`.
- The system MUST reject reserved metadata keys (`tenant_id`, `trace_id`, etc.) in request bodies with `400 VALIDATION_ERROR`.
- Images fetched inline MUST be SSRF-hardened (block private IPs, DNS rebind protection) when `IMAGE_INLINE_REQUIRED=true`.

### NFR-03 · Reliability

| Guarantee | Mechanism |
|-----------|-----------|
| At-least-once Kafka delivery | Transactional outbox pattern; relay retries on failure |
| Idempotent mutations | `Idempotency-Key` header; 24h Valkey dedup cache |
| Fail-open on revocation | Valkey outage → tokens accepted (availability wins) |
| Fail-closed on guardrails | Unknown guardrail decision → block (security wins) |
| DB connection resilience | Neon POOLED endpoint (transaction mode); `/readyz` gates traffic |
| DLQ on Kafka failure | Every non-compact topic has a `.dlq` (30d retention, 3x replication) |

### NFR-04 · Scalability

- All services MUST be stateless (no in-memory shared state required for correctness).
- Services scale horizontally; all state lives in Neon (Postgres), Valkey, or Kafka.
- Neon connection pooling (transaction mode) MUST be used for all app services.
- Partitioning: Kafka topics partitioned by `tenant_id`; consumers can scale per partition.

### NFR-05 · Maintainability

- **Contract immutability:** published contracts are never edited in-place; breaking changes require `v2` alongside `v1`.
- **Dependency order:** services are built and deployed in dependency order (Phase 0 → 1 → 2 → … → 14).
- **No cross-service imports:** services communicate only over HTTP/REST/JSON and Kafka; no shared code libraries.
- **Automated gates:** `contracts/` validator runs in CI on every PR that touches a contract file.

### NFR-06 · Compliance

- GDPR bulk-wipe: `DELETE /v1/gdpr/wipe` in memory-service removes all memories for a principal; emits `cypherx.memory.gdpr.wiped` event.
- Audit log: every sensitive auth action (token mint, revoke, key rotation, quota change) writes to `auth.audit_log`.
- Token storage: session tokens encrypted; API keys stored as Argon2id hashes; signing keys envelope-encrypted in DB.

---

## Assumptions

1. **External Postgres (Neon):** There is no containerized Postgres in the compose stack; the team provisions a Neon project before running the stack.
2. **Doppler for secrets:** All sensitive config is managed via Doppler; operators must have a Doppler project configured for `dev_local`.
3. **End-user identity is external:** CypherX authenticates agents, not end-users. End-user identity (login, billing) is managed by the external `px0` system.
4. **Provider keys are optional locally:** `MOCK_PROVIDERS=true` / `MOCK_EMBEDDINGS=true` allows the stack to run fully offline.
5. **Redpanda is Kafka-compatible:** The platform uses Redpanda locally and in staging; MSK Kafka in production. The difference is transparent to services.

---

## Constraints

1. **Neon POOLED DSN for apps, DIRECT for migrations:** PgBouncer transaction mode is incompatible with session-level advisory locks; migrations MUST use the DIRECT endpoint.
2. **Contract immutability:** Once a contract is published and used by a released service, it cannot be changed in a breaking way. This is a hard engineering constraint enforced by CI.
3. **No BYPASSRLS:** Service DB roles MUST NOT have BYPASSRLS. This is enforced at `*__init.sql` schema creation time.
4. **Prod manual sync:** ArgoCD `prod-apps.yaml` MUST NOT have `syncPolicy.automated`. Human approval is required for all production deployments.
5. **Image tagging:** Docker images MUST use immutable `sha-<sha7>` tags. `latest` is not permitted in gitops manifests.
6. **`logFormat=json` const:** The Helm chart schema has `logFormat` as a `const: "json"` — services cannot opt out of structured logging.
7. **Phase order:** Service development MUST follow the dependency-ordered phase plan. No service may be built before its contract dependencies (Phase 0) are published.
