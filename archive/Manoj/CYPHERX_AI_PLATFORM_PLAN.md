# CypherX AI — Master Platform Plan
> Version 1.0 | Created: 2026-05-22 | Status: Planning

---

## Table of Contents
1. [Vision & Overview](#1-vision--overview)
2. [System Architecture](#2-system-architecture)
3. [Repository Map](#3-repository-map)
4. [Component Plans](#4-component-plans)
   - 4.1 [cypherx-px0 (Existing Core)](#41-cypherx-px0-existing-core)
   - 4.2 [SharedCore / Auth](#42-sharedcore--auth)
   - 4.3 [SharedCore / LLMs](#43-sharedcore--llms)
   - 4.4 [SharedCore / Guardrails](#44-sharedcore--guardrails)
   - 4.5 [SharedCore / Memory](#45-sharedcore--memory)
   - 4.6 [SharedCore / RAG](#46-sharedcore--rag)
   - 4.7 [Tools (MCP Servers)](#47-tools-mcp-servers)
   - 4.8 [Skills](#48-skills)
   - 4.9 [xAgent](#49-xagent)
   - 4.10 [Platform (Management Repo)](#410-platform-management-repo)
   - 4.11 [Frontend](#411-frontend)
   - 4.12 [SDKs](#412-sdks-future)
5. [Cross-Cutting Concerns](#5-cross-cutting-concerns)
6. [Data & Communication Flow](#6-data--communication-flow)
7. [Development Phases (Build Order)](#7-development-phases-build-order)
8. [Design Principles](#8-design-principles)

---

## 1. Vision & Overview

CypherX AI is a **multi-tenant, language-agnostic, agentic platform** that allows building, deploying, and orchestrating intelligent agents at scale. Agents can be used **in isolation or in combination** through an orchestrator, and can communicate with each other using the **A2A (Agent-to-Agent) protocol**.

The platform is split into independently deployable units so each piece can be offered as a **standalone SaaS product** to external customers as well as powering the internal CypherX product suite.

### Core Protocols
| Protocol | Purpose |
|----------|---------|
| **A2A** | Agent-to-Agent communication — standardised task delegation, status callbacks, streaming responses between agents |
| **MCP** | Model Context Protocol — standardised interface for agents to consume Tools and Skills |

### What sits where
| Layer | What it does |
|-------|-------------|
| **cypherx-px0** | Company-wide identity, billing, org management, notifications (already built) |
| **SharedCore** | Standalone SaaS services — Auth, LLMs, Memory, RAG, Guardrails |
| **Tools** | MCP servers (web search, code exec, APIs, databases, etc.) |
| **Skills** | Declarative skill definitions fetched by agents via MCP + RAG |
| **xAgent** | Agent runtime — build, deploy, orchestrate agents |
| **Platform** | Management layer — monitoring, deployments, config for the whole platform |
| **Frontend** | All UI except px0 |
| **SDKs** | Developer SDKs (built after platform stabilises) |

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          EXTERNAL DEVELOPERS / USERS                        │
│                    (via SDKs, REST APIs, A2A endpoints)                     │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────────────┐
│                            FRONTEND REPO                                    │
│   Agent Builder UI │ Orchestration Canvas │ Dashboards │ Skill/Tool Mgmt   │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────────────┐
│                         PLATFORM (Management)                               │
│    Service Registry │ Config Mgmt │ Deployments │ Observability │ Billing   │
└────┬─────────────┬──────────────┬──────────────────────────────────────────┘
     │             │              │
     ▼             ▼              ▼
┌─────────┐  ┌──────────┐  ┌─────────────────────────────────────────────────┐
│  PX0    │  │  xAgent  │  │                  SharedCore                     │
│(Company │  │  Runtime │  │  ┌────────┐ ┌────────┐ ┌──────────┐ ┌───────┐  │
│Identity │  │          │  │  │  Auth  │ │  LLMs  │ │Guardrails│ │Memory │  │
│Billing  │  │ ┌──────┐ │  │  └────────┘ └────────┘ └──────────┘ └───────┘  │
│Notifs)  │  │ │Agent │ │  │                    ┌───────┐                    │
└─────────┘  │ │  A   │ │  │                    │  RAG  │                    │
             │ └──┬───┘ │  │                    └───────┘                    │
             │    │A2A  │  └─────────────────────────────────────────────────┘
             │ ┌──▼───┐ │
             │ │Agent │ │  ┌──────────────────────────────────────────────────┐
             │ │  B   │ │  │                      TOOLS                       │
             │ └──┬───┘ │  │  MCP Server 1 │ MCP Server 2 │ MCP Server N     │
             │    │MCP  │  │  (Web Search) │ (Code Exec)  │ (Custom APIs)    │
             │    ▼     │  └──────────────────────────────────────────────────┘
             │  Tools & │
             │  Skills  │  ┌──────────────────────────────────────────────────┐
             └──────────┘  │                      SKILLS                      │
                           │  Skill Definitions (retrieved via MCP + RAG)     │
                           └──────────────────────────────────────────────────┘
```

---

## 3. Repository Map

```
CypherX AI (workspace root)
│
├── cypherx-px0/              ← Already built. Company identity layer.
│
├── SharedCore/
│   ├── auth/                 ← Agent auth SaaS (standalone)
│   ├── llms/                 ← LLM gateway SaaS (standalone)
│   ├── guardrails/           ← Guardrails SaaS (standalone)
│   ├── memory/               ← Memory SaaS (standalone)
│   └── rag/                  ← RAG SaaS (standalone)
│
├── Tools/                    ← MCP servers (each independently deployable)
│   ├── web-search/
│   ├── code-executor/
│   ├── file-ops/
│   ├── database-connector/
│   └── ...
│
├── Skills/                   ← Skill definitions (language-agnostic YAML/JSON)
│   ├── registry/
│   ├── definitions/
│   └── ...
│
├── xAgent/                   ← Agent runtime and orchestrator
│
├── platform/                 ← Platform management
│
├── frontend/                 ← All UI (except px0)
│
└── SDKs/                     ← Future: Python, JS, Go SDKs
    ├── python/
    ├── typescript/
    └── go/
```

---

## 4. Component Plans

---

### 4.1 cypherx-px0 (Existing Core)

> Already built. The rest of the platform integrates with it.

**What it provides to the platform:**
- User / Organization identity (JWT tokens, org IDs)
- Billing & subscription management (Stripe)
- Notification service (email, in-app alerts)
- Audit log service
- API Gateway with JWT validation

**Integration contract expected by other services:**
- All services must accept `org_id` and `user_id` in request headers or JWTs issued by px0
- Notification triggers via a webhook or message queue
- Audit events published to a shared event bus

---

### 4.2 SharedCore / Auth

> Standalone SaaS: Agent Identity & Access Management

This is **not** user auth (that's px0). This service authenticates **agents** — issuing identities, credentials, and scoped permissions to agents so they can securely consume other services.

#### Core Features

**Agent Identity Management**
- Agent registration & provisioning (create, update, deactivate, delete agents)
- Unique Agent ID (`agent_id`) with metadata (name, version, description, owner org)
- Agent versioning — multiple versions of an agent coexist
- Agent capability declaration (what tools, skills, services it is allowed to use)

**Credential Issuance**
- API key generation per agent (with scopes)
- Short-lived JWT issuance for agent-to-service calls
- Secret rotation with zero-downtime (old + new key valid during rotation window)
- Credential expiry & auto-renewal hooks

**Authorization (RBAC + ABAC)**
- Role definitions: `agent:read`, `agent:write`, `tool:invoke`, `memory:read`, `memory:write`, `rag:query`, `llm:invoke`, `guardrails:check`, etc.
- Attribute-based rules (e.g., allow only if `org_plan == "pro"`)
- Policy-as-code: policies stored as versioned files
- Real-time policy evaluation endpoint

**Multi-tenancy**
- Full tenant isolation — one org's agents cannot access another org's resources
- Tenant-scoped API keys and roles
- Usage quotas per tenant

**Agent-to-Agent Trust**
- Signed identity tokens for A2A calls
- Delegated auth — Agent A can act on behalf of Agent B within declared scope
- Trust chain verification

**Audit & Observability**
- Every auth decision (allow/deny) logged with timestamp, agent ID, resource, action
- Auth metrics exposed (requests/sec, error rates, latency)
- Health check endpoint

**API Surface**
```
POST   /agents                      Register agent
GET    /agents/{agent_id}           Get agent info
DELETE /agents/{agent_id}           Deactivate agent
POST   /agents/{agent_id}/keys      Issue API key
POST   /agents/{agent_id}/token     Issue short-lived JWT
POST   /agents/{agent_id}/rotate    Rotate credentials
POST   /authorize                   Authorization decision (allow/deny)
GET    /policies                    List policies
POST   /policies                    Create/update policy
GET    /health                      Health check
GET    /metrics                     Prometheus metrics
```

---

### 4.3 SharedCore / LLMs

> Standalone SaaS: Unified LLM Gateway

A single API that abstracts all LLM providers. Agents never talk directly to OpenAI, Anthropic, etc. — they go through this gateway.

#### Core Features

**Provider Abstraction**
- Unified request/response schema — one format regardless of underlying provider
- Supported providers (pluggable, add via config): OpenAI, Anthropic, Google (Gemini), Mistral, Groq, Azure OpenAI, AWS Bedrock, local Ollama
- Provider config stored per tenant — tenants can bring their own API keys (BYOK) or use platform-managed keys
- Model aliasing — `fast`, `smart`, `vision`, `code` map to real model IDs per tenant config

**Routing & Fallback**
- Smart routing: route to cheapest model that meets requirements, or fastest, or most capable
- Automatic fallback: if primary provider is down or rate-limited, failover to secondary
- Load balancing across multiple API keys for the same provider
- Priority queue for critical requests vs. background tasks

**API Key Management**
- Platform-managed key pool — keys stored encrypted, rotated automatically
- BYOK (Bring Your Own Key) — tenants register their own keys, gateway proxies through
- Per-key quota tracking and alerting when nearing limits
- Key health monitoring (test calls, latency tracking)

**Rate Limiting & Quota**
- Token-based rate limiting per agent, per tenant, per model
- Burst allowance with smooth throttling
- Hard caps per billing plan
- Real-time quota dashboard

**Cost Tracking**
- Token counting (input + output) per request
- Cost calculation per provider per model
- Per-agent, per-tenant cost roll-up
- Budget alerts (webhook when X% of budget consumed)
- Exportable cost reports

**Caching**
- Semantic caching — identical or near-identical prompts return cached responses
- TTL-based cache invalidation
- Cache bypass option (force fresh call)
- Cache hit rate metrics

**Streaming**
- Server-Sent Events (SSE) for streaming completions
- Chunked transfer support
- Stream interruption handling (client disconnect gracefully handled)

**Request/Response Interceptors**
- Pre-request hook: inject system prompt additions, apply guardrails check before sending
- Post-response hook: run output guardrails, log usage
- Middleware pipeline — composable interceptors

**Observability**
- Latency tracking per provider, per model
- Error rate tracking (provider errors, timeout, etc.)
- Full request/response logging (optional, with PII masking)
- Health endpoint per provider

**API Surface**
```
POST   /chat/completions             Unified chat (streaming + non-streaming)
POST   /completions                  Legacy completion
POST   /embeddings                   Generate embeddings
GET    /models                       List available models
GET    /providers                    List configured providers
POST   /keys                         Register BYOK key
DELETE /keys/{key_id}                Remove key
GET    /usage                        Usage stats
GET    /cost                         Cost breakdown
GET    /health                       Health check
GET    /metrics                      Prometheus metrics
```

---

### 4.4 SharedCore / Guardrails

> Standalone SaaS: AI Safety & Policy Enforcement

Every prompt going **in** to an LLM and every response coming **out** can optionally be passed through Guardrails. This enforces safety, compliance, and business-specific policies.

#### Core Features

**Input Guardrails (Pre-LLM)**
- Prompt injection detection (attempts to override system instructions)
- Jailbreak pattern detection
- PII detection (names, emails, phone numbers, credit cards, SSNs, etc.)
- PII redaction or blocking
- Toxicity / harmful content detection (violence, hate speech, self-harm)
- Topic blocklist (block prompts about restricted subjects)
- Custom keyword/pattern blocklist
- Language restriction (only allow specific languages)
- Length limiting

**Output Guardrails (Post-LLM)**
- Hallucination signal detection (claim verification hooks)
- Toxic / unsafe output detection and blocking
- PII leakage detection in responses
- Format validation (ensure response matches expected JSON schema, etc.)
- Custom output policies
- Response length limits
- Watermarking hooks (flag AI-generated content)

**Policy Engine**
- Policies defined as versioned, declarative files (YAML/JSON)
- Policy sets — group policies and assign to agents or tenants
- Policy inheritance — org-level → agent-level overrides
- Hot reload — policy updates without restart
- Policy simulation mode — test policy against sample input without enforcing

**Custom Rules**
- Regex rule builder
- Semantic similarity rules (block if similar to reference examples)
- Scoring threshold rules (run classifier, block if score > threshold)
- Chain rules (AND / OR / NOT logic between rules)

**Enforcement Modes**
- `block` — reject the request entirely
- `redact` — remove the offending part and continue
- `warn` — allow but flag for review
- `audit` — allow but log for offline review

**Async & Sync Modes**
- Synchronous: inline check before/after LLM call (adds latency)
- Asynchronous: fire-and-forget audit — response goes through, violation logged for human review

**Multi-tenancy**
- Tenant-specific policy sets
- Tenant can define custom policies on top of platform defaults
- Policy isolation — one tenant cannot see another's policies

**Audit & Reporting**
- All violations logged with: agent ID, tenant ID, rule name, severity, input/output snippet
- Violation trend dashboard
- Export for compliance reports

**API Surface**
```
POST   /check/input                  Check input text
POST   /check/output                 Check output text
POST   /check/both                   Check input + output pair
GET    /policies                     List policies
POST   /policies                     Create policy
PUT    /policies/{policy_id}         Update policy
DELETE /policies/{policy_id}         Delete policy
POST   /policies/{policy_id}/simulate Simulate policy against sample
GET    /violations                   List violations log
GET    /health
GET    /metrics
```

---

### 4.5 SharedCore / Memory

> Standalone SaaS: Long-Term AI Memory Manager

LLMs are stateless by default — context windows are limited and conversations are forgotten. Memory gives agents persistent, structured, retrievable memory across sessions and time.

#### Core Features

**Memory Types**
| Type | Description |
|------|-------------|
| **Episodic** | Records of past interactions / events ("what happened in session X") |
| **Semantic** | Facts and knowledge extracted from conversations ("user prefers Python") |
| **Procedural** | Learned workflows and how-to knowledge ("steps the agent took to solve X") |
| **Working** | Short-term context window extension for the current session |

**Memory Operations**
- `store` — add a memory (manual or auto-extracted from conversation)
- `retrieve` — semantic similarity search to find relevant memories
- `update` — update an existing memory fact
- `delete` — remove a memory (GDPR compliance)
- `forget` — bulk wipe memories for a user or agent
- `summarise` — compress older episodic memories into a shorter semantic form

**Auto Memory Extraction**
- LLM-assisted extraction — after each conversation, automatically extract key facts to store
- Entity recognition — extract people, places, preferences, dates automatically
- Importance scoring — not everything gets stored, only high-signal memories
- Deduplication — don't store the same fact twice

**Memory Scoping**
- `global` — available to all agents in the org
- `agent-scoped` — private to a specific agent
- `user-scoped` — tied to an end user across agents
- `session-scoped` — lasts only for the current session

**Retrieval**
- Vector similarity search (semantic retrieval)
- Keyword / metadata filter search
- Time-based retrieval ("what happened last week")
- Hybrid: semantic + filter combined
- Configurable top-k and score threshold
- Relevance re-ranking

**Memory Lifecycle**
- TTL per memory type (e.g., episodic expires after 90 days by default)
- Archiving vs. deletion policy
- Memory consolidation job (periodically compress episodic → semantic)
- Manual memory review / edit UI

**Multi-tenancy**
- Full tenant isolation
- Per-user isolation within a tenant
- Memory quota per tenant / user / agent

**Integrations**
- Pluggable vector store backends (Pinecone, Qdrant, Weaviate, pgvector — swap without API change)
- Pluggable embedding model (via SharedCore/LLMs gateway)
- Hooks to SharedCore/RAG for long-term knowledge indexing

**API Surface**
```
POST   /memories                     Store memory
GET    /memories/{memory_id}         Get specific memory
PUT    /memories/{memory_id}         Update memory
DELETE /memories/{memory_id}         Delete memory
POST   /memories/retrieve            Semantic retrieval
POST   /memories/extract             Auto-extract from conversation
POST   /memories/summarise           Summarise + compress memories
DELETE /memories                     Bulk wipe (with filters)
GET    /health
GET    /metrics
```

---

### 4.6 SharedCore / RAG

> Standalone SaaS: Universal Retrieval-Augmented Generation

Provides document ingestion, indexing, and retrieval. Any agent can retrieve relevant context chunks before calling an LLM. Supports all document types and scales independently.

#### Core Features

**Document Ingestion Pipeline**
- Supported source types: PDF, Word, Markdown, HTML, plain text, JSON, CSV, code files
- Web scraper connector (crawl URLs and index)
- Cloud storage connector (S3, GCS, Azure Blob)
- Database connector (query a DB table, index results)
- API connector (poll external API, index output)
- Webhook / push ingestion (external systems push documents)
- Incremental updates (re-index only changed documents)
- Document versioning — keep history of changes

**Chunking Strategies**
- Fixed-size chunking with overlap
- Sentence-boundary chunking
- Semantic chunking (split by topic shift)
- Recursive chunking (chapter → section → paragraph)
- Custom chunking via config
- Per-document chunking strategy override

**Embedding**
- Pluggable embedding models (via SharedCore/LLMs or direct)
- Batch embedding for large ingestion jobs
- Embedding model versioning — re-embed if model changes

**Vector Storage**
- Pluggable backends: Pinecone, Qdrant, Weaviate, pgvector, Chroma (swap via config)
- Metadata storage alongside vectors
- Namespace / collection per knowledge base
- Multi-vector storage (store multiple chunk representations)

**Retrieval**
- Dense vector search (semantic)
- Sparse keyword search (BM25)
- Hybrid search (dense + sparse combined with configurable weights)
- Metadata filtering (date, source, tags, document type)
- Query expansion (rephrase query multiple ways, combine results)
- Re-ranking (cross-encoder re-rank top-k results)
- Configurable top-k per query
- Minimum relevance score threshold

**Knowledge Bases**
- Multiple knowledge bases per tenant (e.g., one per product, per domain)
- Knowledge base access control (which agents can query which KB)
- KB statistics (document count, chunk count, last updated)

**Multi-modal Support**
- Text extraction from images (OCR)
- Image embedding & retrieval (with vision models)
- Table understanding from PDFs/CSV
- Code-specific chunking and retrieval

**Observability**
- Retrieval latency per query
- Hit rate / miss rate
- Query log for debugging
- Ingestion pipeline status and error reporting

**API Surface**
```
POST   /knowledge-bases                     Create KB
GET    /knowledge-bases                     List KBs
DELETE /knowledge-bases/{kb_id}            Delete KB
POST   /knowledge-bases/{kb_id}/ingest     Ingest document(s)
GET    /knowledge-bases/{kb_id}/documents  List documents
DELETE /knowledge-bases/{kb_id}/documents/{doc_id}  Delete document
POST   /knowledge-bases/{kb_id}/query      Retrieve chunks
GET    /knowledge-bases/{kb_id}/status     Ingestion pipeline status
GET    /health
GET    /metrics
```

---

### 4.7 Tools (MCP Servers)

> Independent MCP servers — each is a separately deployable service

Each tool exposes a standard MCP interface. Agents discover and invoke tools through the MCP protocol. Tools scale independently of each other.

#### Tool Registry & Discovery
- Central registry listing all available tools with metadata (name, version, description, input schema, auth requirements)
- Version pinning — agent can specify exact tool version
- Tool health monitoring — registry knows which tools are healthy
- Tagging & categorisation (search, code, data, communication, etc.)

#### Standard Tool Categories (Phase 1)

| Tool | MCP Server Name | Description |
|------|----------------|-------------|
| Web Search | `tool-web-search` | Search the web, return ranked results with snippets |
| Code Executor | `tool-code-exec` | Execute sandboxed code (Python, JS, shell) |
| File Operations | `tool-file-ops` | Read/write/list files in a sandboxed workspace |
| HTTP Client | `tool-http-client` | Make arbitrary HTTP requests (with auth support) |
| Database Query | `tool-db-query` | Query SQL/NoSQL databases |
| Email | `tool-email` | Send emails via SMTP / provider APIs |
| Calendar | `tool-calendar` | Read/write calendar events |
| Browser Automation | `tool-browser` | Headless browser for web scraping and interaction |
| Image Generation | `tool-image-gen` | Generate images via Stable Diffusion / DALL-E |
| PDF Generator | `tool-pdf-gen` | Generate PDFs from HTML/Markdown |
| Data Analysis | `tool-data-analysis` | Run pandas/SQL style analysis on tabular data |
| Notification Sender | `tool-notify` | Send notifications via px0 notification service |

#### Per-Tool Features (each MCP server must have)
- **MCP-compliant manifest** — `name`, `version`, `description`, `tools[]` with JSON schema per tool
- **Input validation** — reject malformed input before execution
- **Auth** — accept agent JWT from SharedCore/Auth, verify before executing
- **Rate limiting** — prevent abuse per agent
- **Sandboxing** — code execution, file ops run in isolated containers
- **Timeout handling** — every tool invocation has a max execution time
- **Error contract** — standard error response format across all tools
- **Health check** endpoint
- **Metrics** endpoint (invocation count, latency, error rate)
- **Versioning** — semantic versioning, old versions kept available

#### Tool Independence
- Each MCP server is a completely independent deployable unit
- No shared runtime between tools
- Communicate only via MCP protocol (no direct RPC between tools)
- Can be deployed on different infrastructure, different languages

---

### 4.8 Skills

> Declarative skill definitions — retrieved by agents at runtime via MCP + RAG

Skills are **not code** — they are structured definitions (YAML/JSON) that tell an agent *how* to approach a task: which tools to use, what the workflow steps are, what constraints to apply, and what success looks like.

#### Skill Definition Schema
```yaml
skill_id: "research-and-summarise"
version: "1.2.0"
name: "Research and Summarise"
description: "Search the web for information on a topic and produce a structured summary"
tags: [research, summarisation, web]
required_tools:
  - tool-web-search
  - tool-http-client
required_services:
  - llms
  - memory (optional)
input_schema:
  topic: string (required)
  depth: enum[shallow, deep] (default: shallow)
  max_sources: integer (default: 5)
output_schema:
  summary: string
  sources: array[url, title, snippet]
steps:
  - id: search
    action: tool:web-search
    input: "{{input.topic}}"
  - id: fetch
    action: tool:http-client
    input: "{{search.top_urls}}"
  - id: summarise
    action: llm:chat
    prompt_template: "summarise_template_v2"
    input: "{{fetch.content}}"
constraints:
  max_tokens: 2000
  timeout_seconds: 30
guardrails:
  input: ["no-pii"]
  output: ["no-pii", "factuality-check"]
```

#### Skill Registry
- All skills stored and versioned in the Skills repo
- Indexed into SharedCore/RAG for semantic discovery
- Searchable by: name, tags, required tools, description similarity
- Skill validation on publish (schema check, dependency resolution)

#### Skill Retrieval
- Agent asks "what skill should I use for task X?" → RAG query returns matching skills
- MCP tool `tool-skill-retriever` exposes skill search to agents
- Top-k skills returned with relevance scores; agent picks the best fit

#### Skill Lifecycle
- `draft` → `review` → `published` → `deprecated`
- Versioning with backwards compatibility notes
- Community contributions (external developers post-SDK phase)
- Automated testing harness for skills (run skill against mock tools, validate output schema)

#### Skill Categories
- Research & Information Gathering
- Data Processing & Analysis
- Content Generation (writing, summarisation, translation)
- Code Generation & Review
- Workflow Automation
- Customer Support Flows
- Data Extraction (from documents, web pages)
- Multi-agent coordination patterns

---

### 4.9 xAgent

> Agent Runtime — Build, Deploy, Orchestrate, and Communicate

This is the heart of the platform. xAgent provides the runtime that executes agents, handles A2A communication between agents, loads skills and tools via MCP, and manages orchestrated multi-agent workflows.

#### Agent Definition
- Agents defined as config files (YAML/JSON) + optional custom logic hooks
- Config specifies: identity, allowed tools, allowed skills, memory scope, guardrail policies, LLM model preference, system prompt, capabilities
- Agent versioning — immutable versioned deployments
- Agent capability advertisement — each agent declares what tasks it can perform (used by orchestrator for routing)

#### Agent Lifecycle Management
- Provisioning: register agent with SharedCore/Auth, issue credentials
- Deployment: containerised agent runtime, scalable horizontally
- Health monitoring: heartbeat, readiness, liveness probes
- Graceful shutdown with task drain
- Rolling updates without downtime

#### A2A Protocol Implementation
- Every agent exposes a standard A2A endpoint
- Task delegation: Agent A sends a task to Agent B, waits for result or streams progress
- Task types: `sync` (request/response), `async` (fire + poll), `stream` (SSE)
- Agent discovery: query registry for agents that can handle a given task type
- Signed identity — every A2A call carries a signed JWT from SharedCore/Auth
- Retry and circuit-breaker logic for inter-agent calls
- Timeout and cancellation propagation

**A2A Message Schema**
```json
{
  "task_id": "uuid",
  "sender_agent_id": "agent-A",
  "receiver_agent_id": "agent-B",
  "task_type": "research",
  "input": { ... },
  "callback_url": "https://...",
  "stream": false,
  "priority": "normal",
  "timeout_seconds": 60,
  "trace_id": "uuid"
}
```

#### MCP Client
- Built-in MCP client — agents can invoke any registered MCP tool without bespoke integration
- Tool discovery from registry at agent startup or on-demand
- Connection pooling for high-throughput tool calls
- Automatic retry on transient errors
- Tool call timeout enforcement

#### Skill Loading
- At task start, agent queries Skill registry for relevant skills
- Skills loaded and cached in memory for duration of task
- Skill execution: agent follows skill steps, substituting real tool calls
- Skill fallback: if skill tool is unavailable, agent attempts alternate approach

#### Orchestration Engine
- Orchestrator is itself an agent (meta-agent)
- Receives a high-level goal, decomposes into subtasks
- Routes subtasks to specialist agents via A2A
- Supports: sequential, parallel, conditional, looping execution patterns
- Dependency graph execution — subtask B starts only when A completes
- State management — orchestrator tracks all subtask states
- Human-in-the-loop checkpoints — pause for approval before continuing
- Visual workflow representation (consumed by Frontend)

#### Memory Integration
- Per-session working memory (in-process)
- Persistent memory via SharedCore/Memory API (write after each significant step)
- Memory injection into system prompt at task start (relevant long-term facts)
- Configurable memory write policy (always / only if significant / never)

#### Guardrails Integration
- Pre-call guardrail check: every user prompt passed to SharedCore/Guardrails before processing
- Post-call guardrail check: every LLM response checked before returning to user/caller
- Guardrail policy set configurable per agent

#### LLM Integration
- All LLM calls go through SharedCore/LLMs gateway
- Model preference per agent (e.g., Agent A prefers `claude-sonnet`, Agent B prefers `gpt-4o`)
- Token budget per task — agent tracks tokens used, stops if budget exceeded

#### Observability
- Distributed tracing — every task has a `trace_id` propagated through all sub-calls (A2A, tool, LLM)
- Execution timeline: visualise time spent in each step
- Task audit log: full history of inputs, tool calls, LLM calls, outputs
- Cost per task (from SharedCore/LLMs usage data)
- Latency per task and per step
- Error rate, retry count, timeout count

#### External Developer Support
- Agents expose a clean REST + WebSocket API
- A2A endpoint is publicly addressable (with auth)
- Developers can build agents using any language/framework that can speak HTTP + MCP
- Agent manifest published to a public registry (developer marketplace — post-SDK)
- Sandbox environment for testing agents without billing

**Agent API Surface (per agent)**
```
POST   /tasks                        Submit a task
GET    /tasks/{task_id}              Get task status/result
DELETE /tasks/{task_id}             Cancel task
GET    /tasks/{task_id}/stream       Stream task progress (SSE)
GET    /capabilities                 List what this agent can do
GET    /health
GET    /metrics
```

**Orchestrator API**
```
POST   /workflows                    Submit a workflow (goal → decompose → execute)
GET    /workflows/{workflow_id}      Get workflow status
GET    /workflows/{workflow_id}/graph Get execution graph
POST   /workflows/{workflow_id}/approve  Human approval checkpoint
DELETE /workflows/{workflow_id}     Cancel workflow
```

---

### 4.10 Platform (Management Repo)

> Central management layer for the entire CypherX AI platform

The platform repo does **not** deploy or manage px0. It manages everything else: SharedCore services, Tools, Skills, xAgent deployments.

#### Service Registry
- Catalogue of all running services (name, version, health status, endpoints)
- Auto-discovery via service mesh or manual registration
- Dependency graph — know which services depend on which

#### Configuration Management
- Centralised config store (versioned)
- Per-environment configs (dev / staging / prod)
- Secret management (inject secrets into services at deploy time — never in code)
- Config change audit trail
- Hot-reload config propagation

#### Deployment Management
- Deployment definitions for each service (containerised, language-agnostic)
- Rolling deployments with health check gates
- Canary deployments (route X% traffic to new version)
- Rollback in one click
- Environment promotion (dev → staging → prod)

#### Observability Stack
- Centralised log aggregation across all services
- Distributed tracing aggregation (all trace IDs across services)
- Metrics dashboards (per service, per tenant, platform-wide)
- Alerting rules (latency > threshold, error rate > threshold, quota nearing limit)
- On-call alert routing

#### Cost & Billing Integration
- Platform-wide cost tracking (LLM tokens, compute, storage)
- Per-tenant cost roll-up
- Billing events published to px0 for invoice generation
- Cost anomaly detection alerts

#### Security
- Secrets rotation scheduler
- Certificate management
- Network policy management
- Vulnerability scanning integration for container images
- Dependency audit

---

### 4.11 Frontend

> All UI for the CypherX AI platform (excludes px0 UI)

#### Agent Builder
- Visual agent configuration editor (no-code / low-code)
- Define: agent name, system prompt, allowed tools, skills, memory config, guardrail policy
- Test agent inline in the UI (sandbox task execution)
- Publish / version agent from UI
- Agent marketplace browser (browse community agents — post-SDK)

#### Orchestration Canvas
- Visual workflow builder (drag-and-drop)
- Node types: agent node, tool node, condition node, loop node, human-approval node
- Connect nodes with edges (data flow)
- Run workflow from canvas, see execution progress in real time
- Execution timeline view

#### SharedCore Dashboards
- **Auth Dashboard**: registered agents, active keys, auth event log
- **LLMs Dashboard**: usage by model/provider, cost breakdown, latency charts, quota remaining
- **Guardrails Dashboard**: violation log, policy editor, violation trends
- **Memory Dashboard**: memory explorer (browse agent memories), usage stats
- **RAG Dashboard**: knowledge base manager, ingestion status, query testing

#### Skills & Tools Management
- Browse skill library, search by tag/description
- Create/edit skill definitions with schema editor
- Browse MCP tool registry, see tool status and usage
- Test tool invocations from UI

#### Monitoring & Observability
- Real-time task/workflow monitoring
- Agent execution timeline viewer
- Cost per agent / per workflow
- Error and retry explorer
- Distributed trace viewer

#### User & Tenant Management
- Invite team members, assign roles
- API key management (issue/revoke platform API keys)
- Usage & billing overview (embedded from px0 billing data)

---

### 4.12 SDKs (Future)

> To be built once the platform APIs stabilise

**Why wait:** SDK design must mirror stable APIs. Premature SDK locks the API shape.

**Planned SDKs:**
| SDK | Language | Priority |
|-----|----------|---------|
| Python SDK | Python | P1 (ML/AI developer audience) |
| TypeScript SDK | TypeScript/Node | P1 (web/fullstack developers) |
| Go SDK | Go | P2 (infra/backend engineers) |

**SDK Features (per language):**
- Full coverage of xAgent API (create, invoke, orchestrate agents)
- SharedCore service clients (Auth, LLMs, Memory, RAG, Guardrails)
- MCP client helper
- A2A client helper
- Type-safe request/response models auto-generated from API schemas (OpenAPI → codegen)
- Streaming support
- Retry and error handling baked in
- Usage examples and quickstart templates

---

## 5. Cross-Cutting Concerns

These apply to **every** service in the platform.

### API Design Standards
- All APIs: RESTful + JSON (primary), gRPC (optional for high-performance paths)
- All APIs versioned: `/v1/`, `/v2/` — never break existing clients
- OpenAPI 3.0 spec published for every service
- Consistent error response format:
  ```json
  { "error": { "code": "INVALID_INPUT", "message": "...", "details": {} } }
  ```
- Pagination on all list endpoints (cursor-based)
- Idempotency keys on mutation endpoints

### Authentication & Authorization
- Every service validates agent JWTs issued by SharedCore/Auth
- Service-to-service calls also authenticated (internal service tokens)
- No service is ever publicly accessible without auth (no exceptions)

### Multi-Tenancy
- Every database table / collection has `tenant_id` (row-level isolation)
- Every API endpoint resolves tenant from JWT claim
- Cross-tenant data access is architecturally impossible (not just policy)

### Observability Standards
- Every service must expose:
  - `GET /health` — liveness check
  - `GET /ready` — readiness check  
  - `GET /metrics` — Prometheus-format metrics
- Standard log format (structured JSON with: timestamp, service, level, trace_id, tenant_id, message)
- All services propagate `trace_id` and `span_id` headers for distributed tracing

### Scalability Standards
- All services stateless (state in DB/cache only) — scale horizontally by adding replicas
- No in-process shared state between requests
- Async where possible — use message queues for non-real-time operations
- DB connection pooling configured per service

### Modularity Standards
- No direct code imports between services — only HTTP / message queue communication
- Shared contracts (API schemas) versioned and published separately
- Feature flags for gradual rollout of new capabilities
- Every service independently deployable without impacting others

### Security Baseline
- All traffic encrypted in transit (TLS 1.2+)
- Secrets never in code or config files — injected at runtime from secret manager
- Input validation on every endpoint (reject at API boundary)
- Rate limiting on every public endpoint
- Dependency scanning in CI pipeline
- Container images scanned for CVEs before deployment

---

## 6. Data & Communication Flow

### Flow 1: User Sends Task to Agent

```
User → Frontend
  → POST /tasks to xAgent
  → xAgent validates JWT with SharedCore/Auth
  → xAgent calls SharedCore/Guardrails (check input)
  → xAgent retrieves relevant memories from SharedCore/Memory
  → xAgent queries Skills registry for relevant skill
  → xAgent builds prompt (system prompt + memory + skill instructions + user input)
  → xAgent calls SharedCore/LLMs /chat/completions
  → SharedCore/LLMs routes to provider (Anthropic / OpenAI / etc.)
  → Response returned to SharedCore/LLMs
  → xAgent calls SharedCore/Guardrails (check output)
  → xAgent stores significant memory via SharedCore/Memory
  → xAgent returns response to user
```

### Flow 2: Agent Uses a Tool

```
xAgent (executing task)
  → Determines tool is needed (e.g., web search)
  → Looks up tool-web-search in Tool Registry
  → Calls tool-web-search MCP server with agent JWT
  → tool-web-search validates JWT with SharedCore/Auth
  → tool-web-search executes search
  → Returns MCP-formatted result to xAgent
  → xAgent continues task with tool output
```

### Flow 3: Agent-to-Agent (A2A) Task Delegation

```
Orchestrator Agent
  → Receives high-level goal
  → Decomposes into subtasks
  → Discovers specialist agents via Agent Registry
  → Issues signed A2A task to Specialist Agent A
  → Issues signed A2A task to Specialist Agent B (parallel)
  → Waits for both results
  → Combines results
  → Calls SharedCore/LLMs for final synthesis
  → Returns result to caller
```

### Flow 4: RAG-Augmented Response

```
xAgent
  → Receives question that may need knowledge base context
  → Calls SharedCore/RAG /query with the question
  → RAG returns top-k relevant chunks
  → xAgent injects chunks into LLM prompt as context
  → Calls SharedCore/LLMs with enriched prompt
  → Returns grounded response
```

### Flow 5: Skill Retrieval

```
xAgent (task starts)
  → Determines task type from user input
  → Calls tool-skill-retriever MCP tool with task description
  → tool-skill-retriever queries SharedCore/RAG (skills knowledge base)
  → Returns top matching skill definitions
  → xAgent selects best skill, loads its steps
  → Executes steps as structured workflow
```

---

## 7. Development Phases (Build Order)

The build order is designed so that each phase's output is **immediately usable** and the next phase builds on stable foundations.

---

### Phase 0 — Contracts & Standards *(Week 1–2)*
> Zero code, only standards. Every team member aligned before writing a line.

- [ ] Define and publish the standard API error format
- [ ] Define and publish the A2A message schema
- [ ] Define and publish the MCP tool manifest schema
- [ ] Define and publish the Skill definition schema
- [ ] Define and publish the standard JWT claims structure (agent_id, tenant_id, scopes)
- [ ] Define and publish the standard log format
- [ ] Define environment setup guide (local dev, docker-compose baseline)
- [ ] Create shared OpenAPI base template all services extend from

**Output:** `contracts/` directory in platform repo with all schemas and standards documented.

---

### Phase 1 — Foundation: Identity & LLM Gateway *(Week 3–6)*
> Nothing works without auth. Nothing is useful without LLMs.

**Build in parallel:**

#### 1A — SharedCore/Auth
- [ ] Agent registration & provisioning
- [ ] API key issuance & JWT minting
- [ ] Basic RBAC (essential scopes only)
- [ ] `/authorize` endpoint
- [ ] Multi-tenant isolation
- [ ] Health + metrics

#### 1B — SharedCore/LLMs
- [ ] Unified API schema (completions + embeddings)
- [ ] 2 providers integrated (Anthropic + OpenAI as baseline)
- [ ] BYOK support
- [ ] Basic rate limiting
- [ ] Token usage tracking
- [ ] Health + metrics

**Exit criteria:** An agent can register, get a JWT, and use it to call the LLM gateway. Two providers work.

---

### Phase 2 — Safety & Knowledge *(Week 7–10)*
> Guardrails keep it safe. RAG makes it smart.

**Build in parallel:**

#### 2A — SharedCore/Guardrails
- [ ] Input PII detection + redaction
- [ ] Prompt injection detection
- [ ] Toxicity filter
- [ ] Policy definition (YAML)
- [ ] Sync check endpoint
- [ ] Block / redact / warn modes
- [ ] Violation log

#### 2B — SharedCore/RAG
- [ ] Ingestion pipeline (PDF, Markdown, plain text)
- [ ] Chunking (fixed + sentence boundary)
- [ ] One vector store backend integrated
- [ ] Dense vector search
- [ ] Metadata filtering
- [ ] `/query` endpoint

**Exit criteria:** Prompts and responses can be safety-checked. Documents can be ingested and queried.

---

### Phase 3 — Memory & Skills *(Week 11–14)*

**Build in parallel:**

#### 3A — SharedCore/Memory
- [ ] Episodic + semantic memory types
- [ ] Store, retrieve, delete operations
- [ ] Semantic retrieval (vector similarity)
- [ ] User-scoped and agent-scoped memory
- [ ] TTL & expiry
- [ ] Pluggable vector backend

#### 3B — Skills Registry (v1)
- [ ] Skill schema definition (YAML)
- [ ] Skill repo structure
- [ ] Indexing skills into RAG (SharedCore/RAG)
- [ ] `tool-skill-retriever` MCP server (basic)
- [ ] 10–15 foundational skills authored

**Exit criteria:** Agents can store and retrieve memories. Skills can be discovered via semantic search.

---

### Phase 4 — Tools (MCP Servers) *(Week 15–18)*

#### Phase 4 — Tools
- [ ] Tool Registry service (register, discover, health-check tools)
- [ ] `tool-web-search` MCP server
- [ ] `tool-code-exec` MCP server (sandboxed)
- [ ] `tool-http-client` MCP server
- [ ] `tool-file-ops` MCP server
- [ ] MCP auth integration (all tools validate against SharedCore/Auth)
- [ ] Standard error contract across all tools
- [ ] Per-tool metrics

**Exit criteria:** Agents can discover and invoke web search, code execution, HTTP calls, and file operations via MCP.

---

### Phase 5 — xAgent Core *(Week 19–24)*

This is the largest phase. Build incrementally.

#### 5A — Single-Agent Runtime
- [ ] Agent definition schema
- [ ] Agent provisioning (register with SharedCore/Auth)
- [ ] MCP client (invoke tools)
- [ ] Skill loading and execution
- [ ] Memory integration (read/write)
- [ ] LLM integration (via SharedCore/LLMs)
- [ ] Guardrails integration (pre/post LLM)
- [ ] `/tasks` API (submit, status, result)
- [ ] Task tracing (trace_id propagation)

#### 5B — A2A Communication
- [ ] A2A endpoint (accept tasks from other agents)
- [ ] A2A client (send tasks to other agents)
- [ ] Agent discovery registry
- [ ] Signed JWT for A2A calls
- [ ] Sync + async task modes
- [ ] Streaming (SSE)

#### 5C — Orchestrator
- [ ] Orchestrator agent type
- [ ] Goal decomposition (LLM-powered)
- [ ] Subtask routing to specialist agents
- [ ] Sequential + parallel execution
- [ ] Workflow state machine
- [ ] Human-in-the-loop approval checkpoint

**Exit criteria:** A single agent can receive a task, use tools, apply skills, and return a result. Multiple agents can collaborate via A2A.

---

### Phase 6 — Platform Management *(Week 25–28)*

- [ ] Service registry
- [ ] Centralised config management
- [ ] Deployment definitions for all services
- [ ] Centralised log aggregation
- [ ] Metrics dashboards
- [ ] Alerting rules
- [ ] Cost roll-up and billing event publishing to px0

---

### Phase 7 — Frontend *(Week 29–36)*

Build in parallel with Phase 6 where possible.

- [ ] Auth (powered by px0)
- [ ] Agent Builder UI
- [ ] Orchestration Canvas
- [ ] SharedCore dashboards (Auth, LLMs, Guardrails, Memory, RAG)
- [ ] Skills & Tools management UI
- [ ] Task / Workflow monitoring

---

### Phase 8 — Hardening & External Readiness *(Week 37–42)*

- [ ] Security audit across all services
- [ ] Load testing (each SharedCore service + xAgent)
- [ ] Rate limit tuning
- [ ] Documentation site (public API docs for each service)
- [ ] Sandbox environment for external developers
- [ ] Agent marketplace v1

---

### Phase 9 — SDKs *(Week 43+)*

- [ ] Python SDK (xAgent + SharedCore clients)
- [ ] TypeScript SDK
- [ ] SDK documentation + quickstart tutorials
- [ ] SDK examples published to GitHub

---

### Development Phase Summary

```
Phase 0:  Contracts & Standards          ████ Week 1-2
Phase 1:  Auth + LLM Gateway             ████████ Week 3-6
Phase 2:  Guardrails + RAG               ████████ Week 7-10
Phase 3:  Memory + Skills                ████████ Week 11-14
Phase 4:  MCP Tools                      ████████ Week 15-18
Phase 5:  xAgent (Core + A2A + Orch)     ████████████ Week 19-24
Phase 6:  Platform Management            ████████ Week 25-28
Phase 7:  Frontend                       ████████████████ Week 29-36
Phase 8:  Hardening + External           ████████████ Week 37-42
Phase 9:  SDKs                           ██████████ Week 43+
```

---

## 8. Design Principles

These principles govern every decision made building this platform.

| Principle | What it means in practice |
|-----------|--------------------------|
| **Contract-First** | Define the API schema before writing any code. Services evolve around stable contracts. |
| **Language Agnostic** | Services communicate via HTTP/JSON and message queues only. Any language can implement any service. |
| **Independently Deployable** | Every service can be deployed, scaled, and updated without touching any other service. |
| **Tenant-Isolated by Default** | Multi-tenancy is not an afterthought. `tenant_id` is in every query from day one. |
| **Observable by Default** | Health, metrics, and structured logs are mandatory — not optional extras. |
| **Secure by Default** | Auth required on every endpoint. No unauthenticated surface, no exceptions. |
| **Externally Operable** | Every SharedCore service and xAgent must work as a standalone product for external customers — not just as internal glue. |
| **Fail Gracefully** | Every service handles downstream failures (circuit breaker, fallback, timeout) — no cascading failures. |
| **Version Everything** | APIs, agents, skills, tools, policies — all versioned. Old versions kept until explicitly sunset. |
| **Async Where Possible** | Long-running operations use message queues, callbacks, or SSE — never block synchronously for >200ms. |

---

*End of CypherX AI Master Platform Plan*
*This document should be treated as a living document — update it as decisions are made and the architecture evolves.*
