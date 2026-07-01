# CypherX AI — Tech Stack per Service
> Version 1.1 | Created: 2026-05-23
> Stack policy: **Kotlin + Spring Boot + Gradle** for glue/CRUD services. **Python + FastAPI** for AI-bound services and the agent runtime. **Next.js** for UI. **Tools (MCPs) may use any stack** — they are modular and scale independently.

---

## Scope Note — Who "auth-service" Authenticates

`auth-service` in this platform authenticates **agents**, not end users. End-user authentication, organization/identity, billing, and session management all live in **px0** (already built; Kotlin + Spring Boot + Next.js).

- px0 issues **user JWTs** consumed by the Frontend.
- CypherX `auth-service` issues **agent JWTs** (Contract 1) and **service tokens** (Contract 12) consumed by every CypherX backend service.
- The two JWT systems are independent — different issuers, different JWKS endpoints, different audiences. Kong validates both kinds at the edge: user JWTs on `/v1/*` UI routes (px0 JWKS), agent JWTs on `/v1/agents/*`, `/v1/tasks/*` etc. (CypherX auth JWKS).
- Cross-link: when a UI user creates an agent through the Frontend, the request carries a px0 user JWT to CypherX which uses `created_by` on the agent record. From that point on, the agent has its own identity.

---

## Stack Principles

1. **Default to Kotlin + Spring Boot + Gradle (KTS)** — same as px0 — for glue/CRUD services that don't touch AI workloads directly.
2. **Use Python + FastAPI for AI-bound services and the agent runtime.** AI-bound = serves ML models, calls LLM provider SDKs, parses documents, holds embeddings/vectors, or runs the agentic orchestration loop. Co-locating these in one language minimises serialisation friction and lets the team reuse Anthropic/OpenAI client patterns.
3. **Tools (MCP servers) may use ANY stack.** Each tool is a fully independent service that honours the MCP contract (Phase 0 Contract 4 + the `POST /mcp/v1/invoke` endpoint). A tool may be written in Python, Kotlin, Node, Go, Rust, or anything else — it is selected per-tool to fit the tool's job. Tools scale independently, deploy independently, and adding/removing a tool does not affect any other service.
4. **One language per service.** No mixed-language services. If a service needs both AI logic and lots of glue, split it.
5. **Same observability and log format across all stacks.** Contracts 6, 7, 8 don't care which language emits them.

---

## Per-Service Stack

### Core platform services

| # | Service | Namespace | Stack |
|---|---------|-----------|-------|
| 1 | **auth-service** *(authenticates agents — not end users)* | shared-core | **Kotlin + Spring Boot + Gradle** |
| 2 | **llms-gateway** | shared-core | **Python + FastAPI** |
| 3 | **guardrails-service** | shared-core | **Python + FastAPI** |
| 4 | **memory-service** | shared-core | **Python + FastAPI** |
| 5 | **rag-service** | shared-core | **Python + FastAPI** |
| 6 | **agent-runtime** (xAgent core) | xagent | **Python + FastAPI** |
| 7 | **orchestrator** (xAgent) | xagent | **Python + FastAPI** |
| 8 | **a2a-router** (xAgent) | xagent | **Python + FastAPI** |
| 9 | **platform-service** (incl. tool-registry) | platform-mgmt | **Kotlin + Spring Boot + Gradle** |
| 10 | **px0-bridge** | px0-bridge | **Kotlin + Spring Boot + Gradle** |

### Tools (MCP servers, Phase 7)

> Tools are **modular MCP servers**. Each may be implemented in **any stack** of the team's choosing — Python, Kotlin, Node, Go, Rust, etc. The only requirement is honouring the **MCP contract** (Phase 0 Contract 4: manifest, `POST /mcp/v1/invoke`, `GET /mcp/v1/manifest`, `GET /health`, `GET /metrics`) and the standard observability contracts.
>
> Stack is selected per-tool to fit the tool's job. Adding/removing/replacing a tool does not affect any other service.

| # | Tool | Recommended stack | Why |
|---|------|-------------------|-----|
| 11 | **tool-web-search** | any (Kotlin or Python) | Thin HTTP wrapper around SerpAPI / Brave. |
| 12 | **tool-skill-retriever** | any | Wraps RAG query. |
| 13 | **tool-http-client** | any | Outbound HTTP with SSRF allowlist. |
| 14 | **tool-file-ops** | any | S3 read/write/list. |
| 15 | **tool-code-exec** | any (harness) + gVisor sandbox in target lang | Harness language ≠ user-code language. |
| 16 | **tool-email** | any | SMTP / provider APIs. |
| 17 | **tool-calendar** | any | Google / Microsoft Graph. |
| 18 | **tool-browser** | Python (or Node) | Playwright bindings are Python/Node-first. |
| 19 | **tool-image-gen** | any | Proxies image-gen APIs. |
| 20 | **tool-pdf-gen** | any | HTML/Markdown → PDF wrapper. |
| 21 | **tool-data-analysis** | Python | pandas + DuckDB are the natural fit. |
| 22 | **tool-notify** | any | Wraps px0 notification service. |

### Frontend

| # | Component | Stack |
|---|-----------|-------|
| 23 | **frontend** (Agent Builder, dashboards, canvas) | **Next.js + TypeScript** |

### Skills repo (Phase 8)

Not a runtime service — a Git repo with CI.

- **Skill files:** YAML (Contract 11)
- **CI / schema validator:** any language (Python recommended for `jsonschema` + `pyyaml`)

### SDKs (Phase 14)

| # | SDK | Stack |
|---|-----|-------|
| 24 | **Python SDK** | Python |
| 25 | **TypeScript SDK** | TypeScript |
| 26 | **Go SDK** (P2) | Go |

---

## Service Count Summary

| Stack | Services |
|-------|----------|
| **Kotlin + Spring Boot + Gradle** | auth-service, platform-service, px0-bridge |
| **Python + FastAPI** | llms-gateway, guardrails-service, memory-service, rag-service, agent-runtime, orchestrator, a2a-router |
| **Next.js + TypeScript** | frontend |
| **Any stack (MCP-compliant)** | all 12 tool servers |

**Decision rule for a new core service:** *Does it serve ML inference, parse documents, call LLM provider SDKs, hold embeddings/vectors, or run agentic orchestration?* → **Python + FastAPI**. Otherwise → **Kotlin + Spring Boot**.

**Decision rule for a new tool (MCP server):** Pick the stack that best fits the tool's job. The only contract is MCP-compliance — no other service knows or cares what language the tool uses.

---

## Cross-Cutting Tooling

| Concern | Choice |
|---------|--------|
| IaC | Terraform + Terragrunt |
| K8s package management | Helm 3 |
| GitOps | ArgoCD |
| CI | GitHub Actions |
| Secrets | Doppler (now) → HashiCorp Vault (Phase 13) |
| Schema migrations | **Atlas** (Contract 14) — language-agnostic, applies to all services with a DB |
| API contract format | OpenAPI 3.1 (per Contract 10) |
| Kafka schemas | Confluent Schema Registry (Avro or Protobuf) |
| Local dev | **Tilt + kind + Docker Compose** for dependencies (Postgres, Redpanda for Kafka, MinIO for S3) — see Phase 1 Component 17c |
| Container registry | AWS ECR (one repo per service) |

---

## Standard Project Structure

Every project of a given stack follows the same layout. New service = copy the template.

### Kotlin + Spring Boot + Gradle (KTS)

```
<service>/
├── build.gradle.kts                 ← Gradle Kotlin DSL
├── settings.gradle.kts
├── gradle/wrapper/                  ← Gradle wrapper (committed)
├── src/
│   ├── main/kotlin/                 ← service code
│   ├── main/resources/
│   │   ├── application.yaml
│   │   └── logback-spring.xml       ← JSON log format (Contract 6)
│   └── test/kotlin/
├── db/migrations/                   ← Atlas migrations (Contract 14)
├── helm/                            ← K8s Helm chart for this service
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
├── openapi.yaml                     ← Service API contract (Contract 10)
├── Dockerfile                       ← Multi-stage: build (gradle) → runtime (jre)
├── README.md
└── .github/workflows/               ← CI workflows (if monorepo, lives at repo root)
```

**Standard CI:** `./gradlew check` → `./gradlew bootBuildImage` → push to ECR → bump tag in gitops repo.

### Python + FastAPI

```
<service>/
├── pyproject.toml                   ← managed by uv
├── uv.lock
├── src/<service>/                   ← service code
│   ├── __init__.py
│   ├── main.py                      ← FastAPI app
│   ├── api/                         ← route handlers
│   ├── core/                        ← config, logging, observability
│   ├── db/                          ← DB models / queries
│   └── services/                    ← business logic
├── tests/
├── db/migrations/                   ← Atlas migrations (Contract 14)
├── helm/                            ← K8s Helm chart
├── openapi.yaml                     ← published; FastAPI can auto-generate, but commit the rendered file
├── Dockerfile                       ← Multi-stage: build (uv) → runtime (python:3.12-slim)
├── README.md
└── .github/workflows/
```

**Standard CI:** `uv sync` → `uv run ruff check . && uv run mypy src && uv run pytest` → `docker build` → push to ECR → bump tag in gitops repo.

### Next.js + TypeScript

```
frontend/
├── package.json
├── pnpm-lock.yaml
├── next.config.mjs
├── tsconfig.json
├── app/                             ← App Router pages / layouts
├── components/                      ← UI components
├── lib/                             ← API clients, hooks, utils
├── public/                          ← static assets
├── types/generated/                 ← openapi-typescript output (committed)
├── tests/
├── helm/                            ← if hosted as a container; omit for static export
├── Dockerfile                       ← only if hosted as Next.js server
└── .github/workflows/
```

**Standard CI:** `pnpm install --frozen-lockfile` → `pnpm lint && pnpm test && pnpm build` → static export to S3 (CloudFront invalidation) OR push image to ECR.

### MCP Tool Server (any stack)

```
tool-<name>/
├── (stack-specific build files — pyproject.toml / build.gradle.kts / package.json / etc.)
├── src/                             ← tool code
├── tests/
├── helm/                            ← K8s Helm chart
├── manifest.yaml                    ← MCP manifest (Contract 4) — single source of truth
├── openapi.yaml                     ← derived from manifest
├── Dockerfile
├── README.md                        ← MUST document: tool purpose, inputs/outputs, scaling notes
└── .github/workflows/
```

**Required endpoints (Phase 7 Component 2):**
- `POST /mcp/v1/invoke`
- `GET  /mcp/v1/manifest`
- `GET  /health`
- `GET  /ready`
- `GET  /metrics` (Prometheus format)

**Modular guarantees:**
- Each tool has its own ECR repo, its own Helm chart, its own Deployment.
- Tools never call each other.
- Tools never call agent-runtime.
- Tools only call auth-service (to validate JWTs) and external systems (their actual job).
- HPA, replica counts, node-group assignment are all per-tool.

---

## Judgement Calls Worth Noting

Recorded so they don't get re-litigated:

1. **All xAgent services (agent-runtime, orchestrator, a2a-router) = Python + FastAPI.** xAgent is the agentic orchestration heart that calls LLMs, tools, guardrails, memory, and skills in a loop. Co-locating with the Python AI services means one language for the whole AI dataflow path.
2. **memory-service = Python + FastAPI.** First-cycle looks like CRUD, but Phase 6 enhanced (auto-extraction, summarisation) is LLM-adjacent. Python avoids a future migration.
3. **llms-gateway = Python.** First-party Anthropic + OpenAI SDKs and tokenizers land features first in Python.
4. **rag-service = Python.** Document parsing (PyMuPDF, pdfplumber, `unstructured`) and chunking libs are Python-first.
5. **auth-service = Kotlin — and it authenticates *agents*, not end users.** Spring Security + Spring Authorization Server are best-in-class for JWKS rotation, RS256 signing, OAuth2-style flows. End-user auth lives in px0.
6. **platform-service & px0-bridge = Kotlin.** CRUD + Kafka consumers + px0 DTO sharing.
7. **Tools = any stack.** Each MCP server picks its own stack based on the tool's job. The MCP contract is the only thing that matters across tools.
8. **Schema migrations = Atlas for all services.** One tool, one workflow, one CI step — instead of Flyway for JVM + Alembic for Python.
9. **No GraphQL.** Every interface is OpenAPI 3.1 REST. Streaming uses SSE. WebSockets only if a future service genuinely needs bi-directional.

---

## What This Means for px0 Integration

px0 is on **Kotlin + Spring Boot + Gradle + Next.js**. CypherX AI's Kotlin services share that stack directly.

**Kotlin/Spring CypherX services that share infrastructure with px0:**
- DTO/type artifacts — publish a `cypherx-platform-contracts` Maven library both platforms consume.
- Spring Security configuration patterns (JWT validation against shared JWKS).
- Logging / observability config.
- Test helpers (Testcontainers wrappers, fixture builders).

**Python CypherX services (xagent, AI services, memory, rag) integrate via the wire only:**
- HTTP — REST/JSON or SSE. Language doesn't matter at the wire.
- Kafka — events follow Contract 5 envelope; any language consumes them.
- OpenAPI — each service publishes its spec; Kotlin consumers can generate typed clients via `openapi-generator`.

**Auth boundary:**
- px0 issues user JWTs → consumed by the Frontend.
- CypherX `auth-service` issues agent JWTs → consumed by every CypherX backend service.
- Kong routes by path; the two issuers never overlap.

**px0-bridge** lives at the px0 seam — Kotlin/Spring for zero impedance on the px0 side.

---

*End of stack document. Update when a service is added, when a language choice is revisited, or when a new tool is onboarded.*
