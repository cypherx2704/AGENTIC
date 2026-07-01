# 14 · Developer Guide

## Local Setup

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Docker Desktop | ≥ 4.x | Container runtime |
| Git | ≥ 2.40 | Source control |
| Node.js | 22 | BFF development |
| Python | 3.12 | Python service development |
| JDK | 21 | auth-service development |
| uv | latest | Python dependency manager |
| Doppler CLI | latest | Secrets management |
| kubectl | ≥ 1.28 | K8s operations |
| Helm | ≥ 3.14 | Chart management |

### Step-by-Step Setup

```bash
# 1. Clone the workspace
git clone <cypherx-monorepo> Cypher
cd Cypher

# 2. Neon setup
# Go to https://neon.tech → create project "cypherx_platform"
# Note: POOLED endpoint (contains "-pooler") and DIRECT endpoint

# 3. Configure Doppler (optional — can use .env directly)
doppler login
doppler setup --project cypherx-platform --config dev_local

# 4. Configure compose environment
cd infra/compose
cp .env.example .env
# Fill all <<< SET REAL NEON VALUE >>> placeholders

# 5. Run migrations
docker compose --profile migrate up migrate

# 6. Start the stack
docker compose up -d --build

# 7. Verify
curl http://localhost:8080/livez  # auth-service
curl http://localhost:8083/livez  # xagent
curl http://localhost:8085/livez  # llms-gateway
curl http://localhost:8086/livez  # guardrails
open http://localhost:3000         # admin console
```

---

## Repository Structure

```
Cypher/
├── contracts/              # 21 cross-service contracts (source of truth)
│   ├── jwt/                # Contract 1: JWT claims schema
│   ├── api/                # Contract 2: error envelope, Contract 10: OpenAPI base
│   ├── a2a/                # Contract 3: task request/response
│   ├── kafka/              # Contract 5: Kafka event envelope
│   ├── health/             # Contract 7: /livez, /readyz
│   ├── tracing/            # Contract 8: W3C trace headers
│   ├── service-auth/       # Contract 12: service-to-service tokens
│   ├── tenant/             # Contract 13: multi-tenancy model
│   ├── smoke-tests/        # Contract 15: E2E test cases
│   └── ...                 # Contracts 4, 6, 9, 11, 14, 16–21
│
├── archive/                # Planning specs (read-only reference)
│   └── Manoj/
│       ├── phases/         # phase-00.md to phase-14.md (build specs)
│       │   └── amendments/ # plan-fixes.json (OVERRIDES phase docs)
│       ├── CYPHERX_AI_ENTERPRISE_FLOW.md
│       └── stack.md
│
├── infra/
│   ├── compose/            # Local compose stack
│   │   ├── docker-compose.yml
│   │   ├── docker-compose.override.yml
│   │   └── .env.example
│   ├── dev/local/          # Tilt + kind deps-only stack
│   └── terraform/          # AWS IaC (Terraform + Terragrunt)
│
├── charts/
│   └── cypherx-service/    # Base Helm chart
│
├── gitops/                 # ArgoCD App-of-Apps
│   └── envs/
│       ├── dev/
│       ├── staging/
│       └── prod/           # NO syncPolicy.automated here!
│
├── Shared Core/
│   ├── auth/               # Kotlin Spring Boot
│   │   ├── src/
│   │   ├── db/migrations/
│   │   └── CLAUDE.md       # Read this first before working here
│   ├── llms/               # Python FastAPI
│   ├── guardrails/         # Python FastAPI
│   ├── rag/                # Python FastAPI
│   └── memory/             # Python FastAPI
│
├── Tools/
│   ├── tool-registry/      # Python FastAPI
│   └── tool-web-search/    # Python FastAPI
│
├── xAgent/
│   ├── ax-1/               # Python FastAPI (Phase 9A)
│   └── ax-2/               # Empty (Phase 10)
│
├── frontend/
│   ├── app/                # Next.js 15 SPA
│   └── bff/                # Node 22 Fastify BFF
│
├── platform/               # Stub (Phase 11)
├── CoreProjects/
│   └── cypherx-a1/         # Python FastAPI (consuming app)
├── Skills/                 # Empty directory (Phase 8)
└── docs/                   # ← YOU ARE HERE
```

---

## Coding Standards

### All Services
- **Contracts are law.** Never change service code to bypass a contract; always adapt to the contract.
- **Contract 2 errors everywhere.** All error responses use `{"error": {"code", "message", "details"}}`.
- **Contract 6 logging.** All log statements go through the structured logger; never `print()` or `console.log()`.
- **Contract 8 tracing.** Propagate `traceparent` in every HTTP call and Kafka event.
- **No secrets in code.** Secrets come from env vars only; never hardcode credentials.
- **No cross-service imports.** Services share no code; only the wire contract is shared.

### Python Services
```python
# Import style
from app.core.config import settings
from app.models.task import Task
from app.db.connection import get_conn

# Async everywhere (psycopg3, httpx, aiokafka)
async def create_task(conn: AsyncConnection, task: Task) -> UUID:
    await conn.execute("SET LOCAL app.tenant_id = $1", [str(task.tenant_id)])
    ...

# Error handling — always use Contract 2 envelope
from app.core.errors import CypherXError
raise CypherXError("VALIDATION_ERROR", "Field X is required", details={"field": "X"})

# Logging — use structlog
import structlog
log = structlog.get_logger()
log.info("task_completed", task_id=str(task_id), cost_usd=cost, duration_ms=dur)

# Never print() or use logging.getLogger()
```

### Kotlin (auth-service)
```kotlin
// Use Spring Boot conventions
@RestController
@RequestMapping("/v1/agents")
class AgentController(private val agentService: AgentService) {
    
    @PostMapping("/{agentId}/token")
    fun issueToken(
        @PathVariable agentId: UUID,
        @RequestBody request: TokenRequest
    ): ResponseEntity<TokenResponse> { ... }
}

// Contract 2 error handling via @ControllerAdvice
@ExceptionHandler(CypherXException::class)
fun handleCypherXException(ex: CypherXException): ResponseEntity<ErrorEnvelope> { ... }

// Structured logging via logstash-logback-encoder
val log = LoggerFactory.getLogger(AgentController::class.java)
log.info("token_issued", "agent_id" to agentId.toString(), "jti" to jti)
// NEVER: log.info("Issued token for agent $agentId with key $apiKey")
```

### TypeScript (frontend-bff)
```typescript
// Fastify plugin pattern for BFF routes
import type { FastifyPluginAsync } from 'fastify'

export const tasksPlugin: FastifyPluginAsync = async (fastify) => {
  fastify.post('/api/tasks', {
    preHandler: [fastify.authenticate, fastify.checkCsrf],
  }, async (request, reply) => {
    const session = await fastify.getSession(request)
    // Never expose session.jwt to response
    const response = await fastify.proxy('/v1/tasks', {
      Authorization: `Bearer ${session.jwt}`,
      'X-Tenant-ID': session.tenantId,
    })
    return reply.send(response)
  })
}
```

---

## Branching Strategy

```
main ──────────────────────────────────────► (protected, auto-deployed to dev)
  │
  ├── feature/auth-key-rotation ──► PR → review → merge to main
  ├── feature/llms-streaming ──────► PR → review → merge to main
  └── fix/xagent-timeout ──────────► PR → review → merge to main

main → staging (auto via ArgoCD on PR merge)
main → prod (manual PR to gitops/envs/prod/ + human approval + manual ArgoCD sync)
```

**Branch naming:**
- `feature/<scope>-<description>` — new feature
- `fix/<scope>-<description>` — bug fix
- `chore/<description>` — maintenance (deps, docs, tooling)
- `contract/<number>-<description>` — new or updated contract

---

## How to Add a New Service

1. **Read the phase spec** for the new service in `archive/Manoj/phases/phase-XX.md`.

2. **Check `amendments/plan-fixes.json`** — it overrides the phase spec where they conflict.

3. **Create the repo directory:**
   ```bash
   mkdir -p MyService/my-service
   ```

4. **Author `CLAUDE.md`** at `MyService/my-service/CLAUDE.md` following the pattern of existing service CLAUDE.md files. Add it to the repo map in the root `CLAUDE.md`.

5. **Write the migration files:**
   ```bash
   mkdir -p MyService/my-service/db/migrations
   # Create: YYYYMMDD_0001__init.sql (schema + role + tables + RLS)
   # Create: YYYYMMDD_0002__seed.sql (platform defaults)
   ```

6. **Add to compose:**
   - Add service to `infra/compose/docker-compose.yml` with:
     - `healthcheck: GET /readyz`
     - `depends_on` with condition `service_healthy`
     - Port mapping (pick next available from the port map)
     - All required env vars referencing `.env`
   - Add migration mount to the `migrate` job.
   - Add the service DSN to `.env.example` and Doppler.

7. **Add to Helm:**
   - Copy `charts/example-service/` as a template.
   - Use `cypherx-service` as the base chart.

8. **Add to GitOps:**
   - Add an ArgoCD Application to `gitops/envs/dev/` and `gitops/envs/staging/`.
   - **Do not add** `syncPolicy.automated` to `gitops/envs/prod/`.

9. **Implement Contract 7 health endpoints:**
   ```python
   @app.get("/livez")
   async def liveness():
       return {"status": "up", "service": "my-service", "version": VERSION}
   
   @app.get("/readyz")
   async def readiness():
       # check DB, Kafka
       return {"status": "ready"}
   
   @app.get("/metrics")
   async def metrics():
       # Prometheus text format
       return Response(content=generate_latest(), media_type="text/plain")
   ```

10. **Wire up structured logging and tracing** following Contract 6 and Contract 8.

11. **Update `docs/06-services/README.md`** and `docs/README.md` with the new service.

---

## How to Add a New API Endpoint

1. **Check if a contract needs updating** (new request/response schema). If yes, update `contracts/` first and get it merged — contracts gate service PRs.

2. **Add the route handler.** Follow the existing handler pattern for error handling (Contract 2 envelope).

3. **Set `app.tenant_id` at the start of every tenant-scoped handler:**
   ```python
   await conn.execute("SET LOCAL app.tenant_id = $1", [str(tenant_id)])
   ```

4. **Validate inputs against the contract schema.** Never trust the request body for reserved fields.

5. **Emit a Kafka event if the operation mutates state:**
   - Write to the `outbox` table in the same DB transaction as your domain state change.
   - Use Contract 5 envelope format.

6. **Add unit tests** (mock HTTP + DB) and integration tests (real DB).

7. **Update `docs/07-api/README.md`** with the new endpoint.

---

## How to Add a New Kafka Event

1. **Define the event schema** in `contracts/kafka/events/<domain>.<entity>.<event-type>.schema.json`.

2. **Add an example** in `contracts/kafka/events/examples/`.

3. **Run `npm test` in `contracts/`** to validate the schema.

4. **Add the topic** to the compose Redpanda setup (auto-created on first publish if `auto.create.topics.enable=true`; or explicitly in the Redpanda/MSK config).

5. **Write to the outbox table** in the same DB transaction as your state change:
   ```python
   await conn.execute(
       "INSERT INTO outbox (id, topic, key, payload) VALUES ($1, $2, $3, $4)",
       [str(uuid4()), "cypherx.my-domain.my-entity.my-event", tenant_id, 
        json.dumps(event_envelope)]
   )
   ```

6. **Document in `docs/`**: add the topic to `06-services`, `08-database`, and `03-architecture` event flow diagram.

---

## How to Update a Contract

> **Warning:** Contracts are immutable once published. Changing a contract in a breaking way requires a `v2`.

### Adding a new OPTIONAL field (non-breaking)

1. Add the field to the schema with `"required"` list unchanged.
2. Add `"additionalProperties": true` if not already present.
3. Update the example file.
4. Run `npm test` in `contracts/`.
5. Update the CHANGELOG in `contracts/CHANGELOG.md`.
6. Services update to produce/consume the new field — old code still works.

### Breaking change (new `v2`)

1. Create `contracts/<domain>/<name>.v2.schema.json` alongside `v1`.
2. Mark `v1` as deprecated in the CHANGELOG.
3. Services implement `v2` handler.
4. Run both `v1` and `v2` handlers until all callers migrate.
5. Remove `v1` only after all services have migrated and a deprecation window (≥6 months) has passed.

---

## Contribution Guide

### PR Checklist

- [ ] Does this PR touch `contracts/`? → Run `cd contracts && npm test` locally.
- [ ] Does this PR add/change a DB schema? → Include the migration file.
- [ ] Does this PR change an API? → Update `docs/07-api/README.md`.
- [ ] Does this PR add a new service? → Author `CLAUDE.md`, add to compose, update docs.
- [ ] Did you write unit tests for new business logic?
- [ ] Did you run `uv run pytest` (Python) or `./gradlew test` (Kotlin) locally?
- [ ] No secrets committed (check `.env` is not staged)?
- [ ] No hardcoded `tenant_id`, `iss`, `aud`, or URLs in service code?

### Code Review Focus Areas

1. **Security:** Does the handler set `app.tenant_id`? Are reserved fields rejected? Is the JWT verified before trusting claims?
2. **Contract compliance:** Does the response match Contract 2 error format? Are all required fields present?
3. **Outbox:** If the handler mutates state AND needs to emit an event, is it written to `outbox` in the same transaction?
4. **Logging:** No secrets in logs; structured fields only; no print statements.
5. **Tests:** Unit test for business logic; integration test for DB path.

### Commit Message Format

```
<type>(<scope>): <short description>

[optional body]

[optional footer]
```

Types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`

Examples:
```
feat(xagent): add task cancellation endpoint
fix(auth): correct JWT expiry calculation for service tokens
docs(api): add streaming response examples to API docs
chore(contracts): add contract 21 webhook delivery schema
```
