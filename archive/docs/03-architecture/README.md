# 03 · Architecture

## 3.1 System Context Diagram

Shows the full system boundary: who uses CypherX and what external systems it depends on.

```mermaid
C4Context
    title System Context — CypherX AI Platform

    Person(adminUser, "Platform Admin", "Registers agents, monitors usage via admin console")
    Person(agentDev, "Agent Developer", "Builds consuming apps that call CypherX APIs")
    Person(endUser, "End User", "Interacts with an AI product built on CypherX")

    System(cypherx, "CypherX AI Platform", "Multi-tenant agent runtime: auth, LLM gateway, guardrails, RAG, memory, tools, orchestration")

    System_Ext(anthropic, "Anthropic API", "Claude models (Opus, Sonnet, Haiku)")
    System_Ext(openai, "OpenAI API", "GPT-4o, text-embedding-3 models")
    System_Ext(neon, "Neon (Postgres + pgvector)", "Serverless relational DB; all persistent state")
    System_Ext(redpanda, "Redpanda (Kafka)", "Event streaming; transactional outbox relay")
    System_Ext(doppler, "Doppler", "Secrets manager; synced to K8s Secrets in cloud")
    System_Ext(px0, "px0 (External)", "End-user identity, billing, subscription management")
    System_Ext(github, "GitHub Actions / GitLab CI", "CI/CD pipeline; image build → ECR → ArgoCD")

    Rel(adminUser, cypherx, "Manages agents and monitors via admin console", "HTTPS")
    Rel(agentDev, cypherx, "Calls REST APIs with agent JWTs", "HTTPS + SSE")
    Rel(endUser, cypherx, "Interacts via agent-powered products", "HTTPS")
    Rel(cypherx, anthropic, "Routes LLM calls", "HTTPS")
    Rel(cypherx, openai, "Routes LLM calls + embeddings", "HTTPS")
    Rel(cypherx, neon, "Reads/writes all persistent state", "Postgres TLS")
    Rel(cypherx, redpanda, "Publishes domain events", "Kafka TLS")
    Rel(cypherx, doppler, "Pulls secrets at startup", "HTTPS")
    Rel(cypherx, px0, "Reports metered usage for billing", "HTTPS")
    Rel(github, cypherx, "Deploys immutable image tags via GitOps", "HTTPS")
```

---

## 3.2 High-Level Architecture

The platform has four logical tiers:

```mermaid
graph TB
    subgraph "Clients"
        Browser["Browser (SPA)"]
        APIClient["API Client / Agent"]
        Demo["Demo Runner\n(stdlib Python)"]
    end

    subgraph "Edge & BFF"
        Edge["Edge Proxy\nCaddy :8000 (local)\nKong + Istio (cloud)"]
        BFF["frontend-bff\nNode 22 / Fastify\nSession + CSRF + Proxy"]
    end

    subgraph "Shared Core Services"
        Auth["auth-service\nAgent Identity / JWT / OIDC"]
        LLMs["llms-gateway\nUnified LLM / Embeddings / Metering"]
        GR["guardrails-service\nInput/Output Safety Filter"]
        RAG["rag-service\nKnowledge Base / pgvector Retrieval"]
        Mem["memory-service\nPrincipal-Scoped Memory / pgvector"]
    end

    subgraph "Agent Runtime"
        xA1["xAgent / ax-1\nTask Pipeline Runtime (Phase 9A)"]
        xA2["xAgent / ax-2\nA2A Router + Orchestrator (Phase 10 — stub)"]
    end

    subgraph "Tools Layer"
        TR["tool-registry\nMCP Tool Catalogue"]
        TWS["tool-web-search\nMCP web_search Server"]
    end

    subgraph "Data & Messaging"
        Neon["Neon (Postgres + pgvector)"]
        Redpanda["Redpanda (Kafka)"]
        Valkey["Valkey (Redis-compat)"]
        MinIO["MinIO (S3-compat)"]
    end

    subgraph "External"
        Anthropic["Anthropic API"]
        OpenAI["OpenAI API"]
    end

    Browser --> Edge
    APIClient --> Edge
    Demo --> Edge
    Edge --> BFF
    BFF --> Auth
    BFF --> xA1
    xA1 --> Auth
    xA1 --> GR
    xA1 --> LLMs
    xA1 --> RAG
    xA1 --> Mem
    xA1 --> TR
    TR --> TWS
    LLMs --> Anthropic
    LLMs --> OpenAI

    Auth --> Neon
    LLMs --> Neon
    GR --> Neon
    xA1 --> Neon
    RAG --> Neon
    Mem --> Neon
    TR --> Neon
    RAG --> MinIO
    Auth --> Valkey
    BFF --> Valkey
    LLMs --> Valkey

    xA1 --> Redpanda
    Auth --> Redpanda
    LLMs --> Redpanda
    GR --> Redpanda
    RAG --> Redpanda
    Mem --> Redpanda
```

---

## 3.3 Component Diagram

Internal decomposition of the platform into logical components.

```mermaid
graph TB
    subgraph "CypherX AI Platform"
        subgraph "Identity Plane"
            AgentReg["Agent Registry\n(agents, api_keys tables)"]
            JWTMint["JWT Mint Service\n(RS256, signing_keys)"]
            JWKS["JWKS / OIDC Discovery\n(/.well-known/*)"]
            Revoke["Revocation Mirror\n(Valkey + Kafka relay)"]
            Quota["Quota Enforcer\n(quotas, limits tables)"]
            Audit["Audit Log\n(audit_log table)"]
            Webhooks["Webhook Delivery\n(webhooks, webhook_deliveries)"]
        end

        subgraph "LLM Plane"
            ProvRouter["Provider Router\n(Anthropic / OpenAI)"]
            Meter["Token Meter\n(usage_records, llm_call_id dedup)"]
            BYOKMgr["BYOK Manager\n(tenant_provider_keys)"]
            ModelAlias["Model Alias Resolver\n(model_aliases table)"]
            Embed["Embeddings Endpoint\n(/v1/embeddings)"]
        end

        subgraph "Safety Plane"
            InputCheck["Input Checker\n(/v1/check/input)"]
            OutputCheck["Output Checker\n(/v1/check/output)"]
            PolicyEng["Policy Engine\n(rules, policies tables)"]
            Redactor["HMAC Redactor\n(tenant_redaction_keys)"]
            ViolLog["Violation Logger\n(violations table)"]
        end

        subgraph "Agent Plane"
            TaskSched["Task Scheduler\n(tasks, task_steps tables)"]
            StagePipeline["Stage Pipeline\nLOAD→PRE_GR→PROMPT→LLM→POST_GR→EVENT"]
            WP12Stages["WP12 Optional Stages\nRAG_QUERY·MEM_READ·TOOL_LOOP·MEM_WRITE\n(flag-disabled)"]
            Outbox["Outbox Relay\n(outbox table → Kafka)"]
        end

        subgraph "Knowledge Plane"
            KBMgr["KB Manager\n(knowledge_bases, documents)"]
            Ingester["Document Ingester\n(chunks, chunk_vectors_1536)"]
            Retriever["pgvector Retriever\n(cosine similarity)"]
            DocStore["Document Store\n(MinIO / S3)"]
        end

        subgraph "Memory Plane"
            MemStore["Memory Store\n(memories, memory_vectors_1536)"]
            MemSearch["Semantic Search\n(pgvector cosine)"]
            SessionMgr["Session Manager\n(sessions table)"]
            GDPRWipe["GDPR Wipe\n(/v1/gdpr/wipe)"]
        end

        subgraph "Tools Plane"
            ToolCat["Tool Catalogue\n(tools, tool_versions)"]
            HealthPoll["Health Poller\n(tool_health table)"]
            MCPInvoker["MCP Invoker\n(POST /mcp/v1/invoke)"]
        end

        subgraph "Frontend Plane"
            SPAApp["SPA Admin Console\n(Next.js 15)"]
            BFFLayer["BFF\n(Fastify 4, encrypted session, CSRF)"]
        end
    end
```

---

## 3.4 Service Interaction Diagram

How services call each other during a task execution.

```mermaid
sequenceDiagram
    participant BFF as frontend-bff
    participant Auth as auth-service
    participant xA as xAgent/ax-1
    participant GR as guardrails-service
    participant LLM as llms-gateway
    participant RAG as rag-service
    participant Mem as memory-service
    participant TR as tool-registry
    participant TW as tool-web-search

    BFF->>Auth: POST /v1/agents/{id}/token (api_key)
    Auth-->>BFF: {access_token: JWT}

    BFF->>xA: POST /v1/tasks {text, agent_id} + Bearer JWT
    xA->>Auth: GET /.well-known/jwks.json (cached 24h)
    Note over xA: Verify JWT locally

    xA->>GR: POST /v1/check/input {text, task_id}
    GR-->>xA: {decision: allow, check_id}

    Note over xA: (WP12) RAG, Memory retrieve — flag-disabled
    xA->>RAG: POST /v1/kbs/{id}/query
    RAG-->>xA: {results[]}
    xA->>Mem: POST /v1/memories/search
    Mem-->>xA: {memories[]}

    xA->>LLM: POST /v1/chat/completions {model, messages}
    LLM-->>xA: {choices[{message{content, tool_calls}}], usage}

    Note over xA: (WP12) Tool loop — flag-disabled
    xA->>TR: GET /v1/tools/{name}
    TR-->>xA: {endpoint_url}
    xA->>TW: POST /mcp/v1/invoke {tool, params}
    TW-->>xA: {result}
    xA->>LLM: POST /v1/chat/completions (with tool results)
    LLM-->>xA: {choices[{message{content}}]}

    xA->>GR: POST /v1/check/output {text, task_id}
    GR-->>xA: {decision: allow}

    Note over xA: (WP12) Memory write — flag-disabled
    xA->>Mem: POST /v1/memories {content, agent_id}
    Mem-->>xA: {memory_id}

    xA-->>BFF: A2A task response (Contract 3)
```

---

## 3.5 Deployment Diagram — Local Compose Stack

```mermaid
graph TB
    subgraph "Host Machine"
        subgraph "Docker Compose Network: cypherx"
            Edge["edge\nCaddy :8000"]
            Auth["auth-service\n:8080 → host :8080"]
            LLMs["llms-gateway\n:8080 → host :8085"]
            GR["guardrails-service\n:8080 → host :8086"]
            xA["xagent\n:8080 → host :8083"]
            RAG["rag\n:8080 → host :8087"]
            Mem["memory\n:8080 → host :8088"]
            TR["tool-registry\n:8080 → host :8089"]
            TWS["tool-web-search\n:8080 → host :8091"]
            BFF["frontend-bff\n:8088 → host :8092"]
            SPA["frontend-app\n:3000 → host :3000"]
            Demo["demo (profile)\n:8090"]
            RP["redpanda\n:29092 int / :9092 host"]
            VK["valkey\n:6379"]
            MN["minio\n:9000 / :9001 console"]
            OTel["otel-collector (obs)\n:4317 / :4318"]
            Tempo["tempo (obs)\n:3200"]
            Loki["loki (obs)\n:3100"]
            Prom["prometheus (obs)\n:9091 host"]
            Graf["grafana (obs)\n:3001 host"]
        end
        subgraph "External (Neon Cloud)"
            Neon["Neon Postgres\nPOOLED: apps\nDIRECT: migrations"]
        end
    end

    Edge --> Auth
    Edge --> BFF
    Edge --> SPA
    BFF --> Auth
    BFF --> xA
    xA --> Auth
    xA --> GR
    xA --> LLMs
    xA --> RAG
    xA --> Mem
    xA --> TR
    TR --> TWS
    Auth --> Neon
    LLMs --> Neon
    GR --> Neon
    xA --> Neon
    RAG --> Neon
    Mem --> Neon
    TR --> Neon
    RAG --> MN
    Auth --> VK
    BFF --> VK
    LLMs --> VK
    xA --> RP
    Auth --> RP
    LLMs --> RP
    GR --> RP
    RAG --> RP
    Mem --> RP
```

---

## 3.6 Deployment Diagram — Cloud / Kubernetes (AWS EKS)

```mermaid
graph TB
    subgraph "Internet"
        Users["Users / API Clients"]
    end

    subgraph "AWS"
        R53["Route 53\nDNS"]
        ALB["ALB\nL7 Load Balancer"]

        subgraph "EKS Cluster"
            subgraph "ingress ns"
                Kong["Kong API Gateway\nJWT validate, rate-limit, route"]
            end
            subgraph "istio-system ns"
                Istio["Istio Service Mesh\nmTLS, circuit breaker"]
            end
            subgraph "shared-core ns"
                AuthPod["auth-service pods"]
                LLMPod["llms-gateway pods"]
                GRPod["guardrails-service pods"]
                RAGPod["rag-service pods"]
                MemPod["memory-service pods"]
            end
            subgraph "xagent ns"
                xAPod["xagent pods"]
            end
            subgraph "tools ns"
                TRPod["tool-registry pods"]
                TWSPod["tool-web-search pods"]
            end
            subgraph "platform-mgmt ns"
                PlatPod["platform pods (stub)"]
            end
            subgraph "data ns"
                PgBouncer["PgBouncer\nConnection Pooler"]
            end
            subgraph "messaging ns"
                MSK["MSK (Kafka)\nManaged Kafka"]
            end
            subgraph "observability ns"
                OTel2["OTel Collector"]
                Tempo2["Tempo"]
                Loki2["Loki"]
                Prom2["Prometheus"]
                Graf2["Grafana"]
            end
            subgraph "argocd ns"
                ArgoCD["ArgoCD\nApp-of-Apps GitOps"]
            end
        end

        subgraph "AWS Managed Services"
            RDS["RDS Postgres\n+ pgvector"]
            ElastiCache["ElastiCache\nValkey"]
            S3["S3\nObject Storage"]
            ECR["ECR\nContainer Registry"]
            KMS["KMS\nKey Management"]
            SecretsManager["Secrets Manager\n(Doppler sync target)"]
        end
    end

    Users --> R53
    R53 --> ALB
    ALB --> Kong
    Kong --> Istio
    Istio --> AuthPod
    Istio --> LLMPod
    Istio --> GRPod
    Istio --> RAGPod
    Istio --> MemPod
    Istio --> xAPod
    Istio --> TRPod
    Istio --> TWSPod
    AuthPod --> PgBouncer
    LLMPod --> PgBouncer
    GRPod --> PgBouncer
    xAPod --> PgBouncer
    RAGPod --> PgBouncer
    MemPod --> PgBouncer
    TRPod --> PgBouncer
    PgBouncer --> RDS
    AuthPod --> ElastiCache
    LLMPod --> ElastiCache
    RAGPod --> S3
    xAPod --> MSK
    AuthPod --> MSK
    LLMPod --> MSK
    ECR --> ArgoCD
```

---

## 3.7 Network Diagram

```mermaid
graph LR
    subgraph "Public Zone"
        Internet["Internet"]
    end

    subgraph "DMZ (Edge)"
        ALB["ALB / Kong\nTLS termination\nJWT validation\nRate limiting"]
    end

    subgraph "Service Mesh (Istio mTLS)"
        direction TB
        subgraph "shared-core"
            Auth2["auth-service"]
            LLMs2["llms-gateway"]
            GR2["guardrails"]
            RAG2["rag"]
            Mem2["memory"]
        end
        subgraph "agent-runtime"
            xA2["xagent"]
        end
        subgraph "tools"
            TR2["tool-registry"]
            TWS2["tool-web-search"]
        end
    end

    subgraph "Data Zone (Private Subnets)"
        RDS2["RDS PostgreSQL\n(Multi-AZ)"]
        MSK2["MSK Kafka\n(Multi-broker)"]
        EC2["ElastiCache Valkey\n(cluster mode)"]
        S32["S3 Bucket\n(VPC endpoint)"]
    end

    subgraph "External APIs (HTTPS)"
        Anth2["Anthropic API"]
        OAI2["OpenAI API"]
    end

    Internet --> ALB
    ALB -->|mTLS| Auth2
    ALB -->|mTLS| xA2
    xA2 -->|mTLS| Auth2
    xA2 -->|mTLS| GR2
    xA2 -->|mTLS| LLMs2
    xA2 -->|mTLS| RAG2
    xA2 -->|mTLS| Mem2
    xA2 -->|mTLS| TR2
    TR2 -->|mTLS| TWS2
    LLMs2 -->|HTTPS| Anth2
    LLMs2 -->|HTTPS| OAI2
    Auth2 --> RDS2
    LLMs2 --> RDS2
    xA2 --> RDS2
    GR2 --> RDS2
    RAG2 --> RDS2
    Mem2 --> RDS2
    TR2 --> RDS2
    xA2 --> MSK2
    Auth2 --> MSK2
    LLMs2 --> MSK2
    Auth2 --> EC2
    LLMs2 --> EC2
    RAG2 --> S32
```

---

## 3.8 Data Flow Diagram

How data flows through the system from a task submission.

```mermaid
flowchart LR
    A["Browser\nHTTPS"] --> B["Edge\nCaddy/Kong"]
    B --> C["BFF\nSession decrypt\nJWT inject"]
    C --> D["xAgent\nTask record create\nJWT re-verify"]
    D --> E["Guardrails\nInput check\nViolation log"]
    E -->|allow| F["RAG\nContext retrieval\npgvector query"]
    F --> G["Memory\nHistory retrieval\npgvector query"]
    G --> H["llms-gateway\nProvider normalize\nToken meter"]
    H --> I["Provider\nAnthropicOpenAI"]
    I --> H
    H --> J["xAgent\nResponse receive\nCost record"]
    J --> K["Guardrails\nOutput check"]
    K -->|allow| L["Memory\nResponse store"]
    L --> M["xAgent\nTask complete\nOutbox write"]
    M --> N["Kafka\nEvent publish"]
    N --> O["Downstream\nBilling / Analytics"]
    M --> P["BFF\nA2A response"]
    P --> A
```

---

## 3.9 Event Flow Diagram

```mermaid
flowchart TB
    subgraph "Producers (Transactional Outbox)"
        A1["auth-service\nauthor: agent.registered\ntenant.created\ntoken.revoked"]
        L1["llms-gateway\nauthor: request.completed\nusage.recorded"]
        G1["guardrails\nauthor: violation.detected\nusage.recorded"]
        X1["xagent\nauthor: task.completed\ntask.failed\ntools.invocation.metered"]
        R1["rag\nauthor: ingestion.requested\ningestion.completed\nusage.recorded"]
        M1["memory\nauthor: stored\ndeleted\ngdpr.wiped"]
    end

    subgraph "Redpanda (Kafka)"
        T1["cypherx.auth.*\n(7d / COMPACT)"]
        T2["cypherx.llms.*\n(30d–90d)"]
        T3["cypherx.guardrails.*\n(30d–90d)"]
        T4["cypherx.agent.task.*\n(30d)"]
        T5["cypherx.rag.*\n(30d–90d)"]
        T6["cypherx.memory.*\n(30d–90d)"]
        DLQ["*.dlq topics\n(30d, 3x replication)"]
    end

    subgraph "Consumers"
        B1["Billing System\n(px0)"]
        B2["Analytics Pipeline"]
        B3["Audit Archive"]
        B4["RAG Ingest Worker\n(ingestion.requested → process)"]
        B5["Revocation Mirror\n(token.revoked → Valkey update)"]
    end

    A1 --> T1
    L1 --> T2
    G1 --> T3
    X1 --> T4
    R1 --> T5
    M1 --> T6

    T1 -->|on failure| DLQ
    T2 -->|on failure| DLQ
    T3 -->|on failure| DLQ
    T4 -->|on failure| DLQ
    T5 -->|on failure| DLQ
    T6 -->|on failure| DLQ

    T2 --> B1
    T4 --> B1
    T5 --> B1
    T6 --> B1
    T2 --> B2
    T4 --> B2
    T1 --> B3
    T3 --> B3
    T5 --> B4
    T1 -->|token.revoked| B5
```

---

## 3.10 Architecture Decisions

Full ADR index: [15 · ADRs](../15-adrs/README.md)

| ADR | Decision |
|-----|---------|
| [ADR-001](../15-adrs/ADR-001-postgresql.md) | PostgreSQL as the primary database |
| [ADR-002](../15-adrs/ADR-002-kafka.md) | Kafka (Redpanda) for event streaming |
| [ADR-003](../15-adrs/ADR-003-kong-istio.md) | Kong + Istio for cloud API gateway and service mesh |
| [ADR-004](../15-adrs/ADR-004-jwt-rs256.md) | RS256 JWT for authentication (not HS256) |
| [ADR-005](../15-adrs/ADR-005-multi-tenant-rls.md) | Postgres RLS for multi-tenant isolation |
| [ADR-006](../15-adrs/ADR-006-contract-first.md) | Contract-first design with immutable versioning |
| [ADR-007](../15-adrs/ADR-007-transactional-outbox.md) | Transactional outbox pattern for Kafka reliability |
| [ADR-008](../15-adrs/ADR-008-openai-schema.md) | OpenAI-superset schema as the LLM gateway wire format |
| [ADR-009](../15-adrs/ADR-009-neon-serverless.md) | Neon serverless Postgres for local + staging DB |
| [ADR-010](../15-adrs/ADR-010-python-fastapi.md) | Python + FastAPI for all non-auth services |
