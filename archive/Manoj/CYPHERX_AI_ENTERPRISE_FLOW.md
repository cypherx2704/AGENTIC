# CypherX AI — Enterprise Architecture Flow Plan
> Version 1.0 | Created: 2026-05-22 | Status: Architecture Definition
> Scope: Platform Foundation & Infrastructure Layer (Pre-Service Implementation)

---

## Technology Stack Decision Record

| Layer | Technology | Reason |
|-------|-----------|--------|
| Cloud Provider | **AWS** (via abstraction layer) | Initial provider; abstraction enables future GCP/Azure migration |
| Container Orchestration | **Kubernetes (EKS)** | Industry standard; cloud-agnostic workloads |
| API Gateway | **Kong Gateway** | Cloud-agnostic, K8s-native, rich plugin ecosystem |
| Service Mesh | **Istio** | mTLS, circuit breaking, traffic management, distributed tracing |
| Event Bus | **Apache Kafka (MSK)** | High-throughput, durable, replay-capable async backbone |
| Observability | **Prometheus + Grafana + Loki + Tempo** | Full open-source stack; self-hosted; cloud-agnostic |
| Secrets (now) | **Doppler** | Quick DX; syncs to K8s Secrets |
| Secrets (future) | **HashiCorp Vault** (migration path defined below) | Cloud-agnostic vault for production hardening |
| Primary DB | **PostgreSQL (RDS)** | ACID, multi-tenant row isolation, battle-tested |
| Cache / Pub-Sub | **Valkey (Redis OSS fork)** | Open-source, BSL-free; rate limiting, caching |
| IaC | **Terraform + Terragrunt** | Cloud-agnostic modules; DRY multi-env configs |
| CI/CD | **GitHub Actions + ArgoCD** | GitHub Actions for build; ArgoCD for GitOps K8s sync |
| Connection Pooling | **PgBouncer** | PostgreSQL connection pooling for all services |

---

## Table of Contents

1. [Platform Topology — Bird's Eye View](#1-platform-topology--birds-eye-view)
2. [Cloud Abstraction Layer](#2-cloud-abstraction-layer)
3. [Kubernetes Cluster Topology](#3-kubernetes-cluster-topology)
4. [Network & Ingress Architecture](#4-network--ingress-architecture)
5. [Service Mesh Architecture (Istio)](#5-service-mesh-architecture-istio)
6. [Async Event Architecture (Kafka)](#6-async-event-architecture-kafka)
7. [Data Layer Architecture](#7-data-layer-architecture)
8. [Observability Architecture](#8-observability-architecture)
9. [Secret & Config Architecture](#9-secret--config-architecture)
10. [CI/CD Pipeline Architecture](#10-cicd-pipeline-architecture)
11. [Request & Data Flow Diagrams](#11-request--data-flow-diagrams)
12. [Namespace & Service Layout](#12-namespace--service-layout)
13. [IaC Repository Structure](#13-iac-repository-structure)
14. [Platform Foundation Setup Order](#14-platform-foundation-setup-order)

---

## 1. Platform Topology — Bird's Eye View

This is the full platform as a single diagram. Every box is a distinct layer; arrows show direction of data/control flow.

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                          EXTERNAL CLIENTS & DEVELOPERS                          ║
║              (Web App · Mobile · SDK users · External Agents via A2A)           ║
╚═══════════════════════════════════╤══════════════════════════════════════════════╝
                                    │ HTTPS / WSS / SSE
                                    ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║                         EDGE LAYER (AWS, abstracted)                            ║
║   Route53 (DNS) ──► AWS ALB (L7 Load Balancer) ──► CloudFront (CDN/DDoS)       ║
╚═══════════════════════════════════╤══════════════════════════════════════════════╝
                                    │
                                    ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║                    KONG API GATEWAY (cloud-agnostic)                            ║
║  ┌─────────────────────────────────────────────────────────────────────────┐    ║
║  │  JWT Validation · Rate Limiting · Request Routing · Plugin Pipeline     │    ║
║  │  API Versioning (/v1/, /v2/) · Auth pre-check · Request Logging        │    ║
║  └─────────────────────────────────────────────────────────────────────────┘    ║
╚════════╤═══════════════════╤════════════════════╤═══════════════════════════════╝
         │                   │                    │
         ▼                   ▼                    ▼
  Platform APIs        xAgent APIs          SharedCore APIs
  /platform/*          /agents/*            /auth/* /llms/*
                        /tasks/*             /rag/*  /memory/*
                        /workflows/*         /guardrails/*
         │                   │                    │
         └───────────────────┴────────────────────┘
                             │
                             ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║                    ISTIO SERVICE MESH (cloud-agnostic)                          ║
║  ┌──────────────────────────────────────────────────────────────────────────┐   ║
║  │  mTLS (all inter-service traffic encrypted) · Circuit Breaker            │   ║
║  │  Traffic Policies · Retry Logic · Load Balancing · Trace Injection      │   ║
║  └──────────────────────────────────────────────────────────────────────────┘   ║
║                                                                                  ║
║  ┌──────────────────────────────────────────────────────────────────────────┐   ║
║  │                   KUBERNETES (EKS) — APPLICATION LAYER                  │   ║
║  │                                                                          │   ║
║  │  ┌────────────────────┐   ┌────────────────────┐   ┌────────────────┐  │   ║
║  │  │    SharedCore       │   │      xAgent        │   │     Tools      │  │   ║
║  │  │  ┌──────────────┐  │   │  ┌──────────────┐  │   │  ┌──────────┐ │  │   ║
║  │  │  │    Auth      │  │   │  │ Agent Runtime │  │   │  │MCP Srv 1 │ │  │   ║
║  │  │  │    LLMs      │  │   │  │ Orchestrator  │  │   │  │MCP Srv 2 │ │  │   ║
║  │  │  │  Guardrails  │  │   │  │  A2A Router   │  │   │  │MCP Srv N │ │  │   ║
║  │  │  │    Memory    │  │   │  └──────────────┘  │   │  └──────────┘ │  │   ║
║  │  │  │     RAG      │  │   │                    │   │                │  │   ║
║  │  │  └──────────────┘  │   └────────────────────┘   └────────────────┘  │   ║
║  │  └────────────────────┘                                                  │   ║
║  │                                                                          │   ║
║  │  ┌──────────────────────────────────────────────────────────────────┐   │   ║
║  │  │                 Platform Management Service                       │   │   ║
║  │  │    Service Registry · Config Store · Deployment Manager          │   │   ║
║  │  └──────────────────────────────────────────────────────────────────┘   │   ║
║  └──────────────────────────────────────────────────────────────────────────┘   ║
╚══════════════════════════════════════════════════════════════════════════════════╝
         │                   │                    │                   │
         ▼                   ▼                    ▼                   ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║                        PLATFORM INFRASTRUCTURE LAYER                            ║
║                                                                                  ║
║  ┌──────────────┐  ┌────────────┐  ┌────────────┐  ┌─────────────────────────┐ ║
║  │    Kafka     │  │ PostgreSQL │  │   Valkey   │  │     Observability       │ ║
║  │  (MSK/AWS)   │  │  (RDS)     │  │  (Cache)   │  │  Prometheus · Grafana  │ ║
║  │              │  │ PgBouncer  │  │            │  │  Loki · Tempo          │ ║
║  └──────────────┘  └────────────┘  └────────────┘  └─────────────────────────┘ ║
║                                                                                  ║
║  ┌──────────────┐  ┌────────────┐  ┌──────────────────────────────────────────┐ ║
║  │   Doppler    │  │   ArgoCD   │  │          AWS (Abstraction Layer)         │ ║
║  │  (Secrets)   │  │  (GitOps)  │  │  EKS · MSK · RDS · S3 · ECR · Route53  │ ║
║  └──────────────┘  └────────────┘  └──────────────────────────────────────────┘ ║
╚══════════════════════════════════════════════════════════════════════════════════╝
         │
         ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║                           cypherx-px0 (Existing)                                ║
║           Identity · Billing · Org Management · Notifications                   ║
║                (Integrates via event bus + REST API contracts)                   ║
╚══════════════════════════════════════════════════════════════════════════════════╝
```

---

## 2. Cloud Abstraction Layer

### Philosophy
Services **never call AWS APIs directly**. Every cloud interaction goes through a **Provider Interface** defined in the platform. AWS is the first implementation. When GCP or Azure support is added, only the implementation changes — no service code changes.

### Provider Interface Design

```
Cloud Abstraction Layer
│
├── IComputeProvider          (K8s cluster lifecycle)
│   ├── getClusterCredentials(clusterName) → KubeConfig
│   └── getNodeGroupStatus(clusterName)  → NodeGroupInfo
│
├── IStorageProvider          (Object storage)
│   ├── createBucket(name, region)
│   ├── uploadObject(bucket, key, data)
│   ├── getObject(bucket, key)          → Stream
│   ├── deleteObject(bucket, key)
│   └── generatePresignedUrl(bucket, key, ttl) → URL
│
├── IDatabaseProvider         (Managed DB lifecycle — ops only)
│   ├── getConnectionString(instanceId) → DSN
│   └── getReplicaConnectionString(instanceId) → DSN
│
├── IMessagingProvider        (Kafka/MSK operations)
│   ├── createTopic(name, partitions, replication)
│   ├── deleteTopic(name)
│   └── getBootstrapBrokers(clusterId)  → []string
│
├── ISecretProvider           (Secret read/write — runtime only)
│   ├── getSecret(name) → string
│   ├── setSecret(name, value)
│   └── rotateSecret(name)
│
└── IRegistryProvider         (Container image registry)
    ├── getLoginCredentials() → DockerAuthConfig
    └── getImageUri(repo, tag) → string
```

### Current AWS Mapping

| Interface | AWS Implementation | Future Alt |
|-----------|-------------------|------------|
| `IComputeProvider` | EKS API | GKE / AKS |
| `IStorageProvider` | S3 + pre-signed URLs | GCS / Azure Blob |
| `IDatabaseProvider` | RDS PostgreSQL | Cloud SQL / Azure DB |
| `IMessagingProvider` | MSK (Managed Kafka) | Confluent Cloud / self-hosted |
| `ISecretProvider` | Doppler → K8s Secrets | Vault / GCP Secret Manager |
| `IRegistryProvider` | ECR | GAR / ACR / DockerHub |

### Implementation Rule
```
services/
└── infra-adapters/
    ├── interfaces/           ← Go interfaces (source of truth)
    │   ├── compute.go
    │   ├── storage.go
    │   ├── database.go
    │   ├── messaging.go
    │   ├── secrets.go
    │   └── registry.go
    └── providers/
        ├── aws/              ← Current implementation
        │   ├── compute.go
        │   ├── storage.go
        │   └── ...
        ├── gcp/              ← Stub (implement when needed)
        └── azure/            ← Stub (implement when needed)
```

---

## 3. Kubernetes Cluster Topology

### Cluster Strategy

```
AWS Region: us-east-1 (primary)
│
└── EKS Cluster: cypherx-prod
    │
    ├── Node Group: system-nodes       (t3.medium × 3, On-Demand)
    │   └── Runs: Istio control plane, Kong, ArgoCD, Cert-Manager
    │
    ├── Node Group: core-services      (c5.xlarge × 3–10, autoscale)
    │   └── Runs: SharedCore services, Platform Management
    │
    ├── Node Group: agent-runtime      (c5.2xlarge × 2–20, autoscale)
    │   └── Runs: xAgent, Orchestrator, A2A Router
    │
    ├── Node Group: tools              (c5.large × 2–8, autoscale)
    │   └── Runs: MCP Tool servers
    │
    └── Node Group: observability      (m5.large × 2, On-Demand)
        └── Runs: Prometheus, Grafana, Loki, Tempo
```

### Namespace Architecture

```
Namespace Strategy: One namespace per functional domain, strict NetworkPolicy isolation

cypherx-prod cluster
│
├── ns: ingress             ← Kong + Istio Ingress Gateway
│   └── NetworkPolicy: accepts inbound from internet via ALB only
│
├── ns: istio-system        ← Istio control plane (Istiod, Kiali)
│   └── NetworkPolicy: accepts from all namespaces (sidecar injection)
│
├── ns: shared-core         ← All SharedCore services
│   ├── auth-service
│   ├── llms-gateway
│   ├── guardrails-service
│   ├── memory-service
│   └── rag-service
│
├── ns: xagent              ← Agent runtime and orchestration
│   ├── agent-runtime
│   ├── orchestrator
│   └── a2a-router
│
├── ns: tools               ← MCP Tool servers
│   ├── tool-web-search
│   ├── tool-code-exec
│   ├── tool-http-client
│   └── tool-file-ops
│
├── ns: platform-mgmt       ← Platform management service
│
├── ns: data                ← PostgreSQL (via External Operator), PgBouncer, Valkey
│   └── NetworkPolicy: accepts ONLY from shared-core, xagent, tools, platform-mgmt
│
├── ns: messaging           ← Kafka (external MSK; ConfigMap with broker addresses)
│
├── ns: observability       ← Prometheus, Grafana, Loki, Tempo
│   └── NetworkPolicy: scrapes from all namespaces
│
├── ns: argocd              ← ArgoCD GitOps operator
│
└── ns: px0-bridge          ← Integration adapters for cypherx-px0
```

### Pod Resource & Scaling Standards

Every service **must** define these in its Helm chart:

```yaml
# Standard template (values.yaml per service)
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "1000m"
    memory: "512Mi"

autoscaling:
  enabled: true
  minReplicas: 2          # Always 2 minimum for HA
  maxReplicas: 20
  targetCPUUtilizationPercentage: 70

podDisruptionBudget:
  minAvailable: 1         # Always at least 1 pod up during rolling deploy

affinity:
  podAntiAffinity:        # Spread across AZs
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: topology.kubernetes.io/zone
```

---

## 4. Network & Ingress Architecture

### Traffic Path: External → Services

```
Internet
  │
  ▼
Route53 (DNS)
  │  A record → ALB DNS name
  ▼
AWS ALB (Layer 7)
  │  SSL termination at ALB
  │  Listeners: :443 → forward to Kong NodePort
  │  Listeners: :80  → redirect to :443
  ▼
Kong Gateway (ns: ingress)
  │
  ├── Plugin: JWT Validation (using px0-issued JWT public keys)
  ├── Plugin: Rate Limiting (per consumer, per route)
  ├── Plugin: Request Logging (structured JSON → Loki)
  ├── Plugin: CORS
  ├── Plugin: IP Restriction (optional per route)
  │
  ├── Route: /v1/auth/*        → shared-core/auth-service
  ├── Route: /v1/llms/*        → shared-core/llms-gateway
  ├── Route: /v1/guardrails/*  → shared-core/guardrails-service
  ├── Route: /v1/memory/*      → shared-core/memory-service
  ├── Route: /v1/rag/*         → shared-core/rag-service
  ├── Route: /v1/agents/*      → xagent/agent-runtime
  ├── Route: /v1/tasks/*       → xagent/agent-runtime
  ├── Route: /v1/workflows/*   → xagent/orchestrator
  ├── Route: /v1/tools/*       → platform-mgmt (tool registry proxy)
  └── Route: /v1/platform/*    → platform-mgmt
  │
  ▼
Istio Ingress Gateway (ns: ingress)
  │  mTLS enforcement begins here
  ▼
Target Service Pod (sidecar: Envoy proxy)
```

### Kong Configuration Principles

```yaml
# Kong Route example (declarative config via KongIngress CRD)
services:
  - name: auth-service
    url: http://auth-service.shared-core.svc.cluster.local:8080
    routes:
      - name: auth-route
        paths: ["/v1/auth"]
        strip_path: true
    plugins:
      - name: jwt          # Validate JWT signature
      - name: rate-limiting
        config:
          minute: 1000
          policy: redis    # Valkey as rate limit backend
      - name: request-id   # Inject X-Request-ID header
      - name: correlation-id
```

### TLS Strategy

```
External TLS (ALB → Kong):   AWS ACM certificate (auto-renewed)
Kong → Internal services:    Istio mTLS (automatic, cert-managed by Istio CA)
Internal service-to-service: Istio mTLS (automatic via sidecar injection)

Certificate Authority hierarchy:
  Istio CA (Istiod) ──► per-service SPIFFE identity certificates
  Each service identity: spiffe://cluster.local/ns/<namespace>/sa/<service-account>
```

---

## 5. Service Mesh Architecture (Istio)

### What Istio Handles (Automatically)
Every pod with `istio-injection: enabled` label on its namespace gets an **Envoy sidecar** injected automatically. This sidecar handles:

```
Pod A                                    Pod B
┌─────────────────┐                    ┌─────────────────┐
│  App Container  │                    │  App Container  │
│  (port 8080)    │                    │  (port 8080)    │
│        │        │                    │        ▲        │
│        ▼        │                    │        │        │
│ Envoy Sidecar   │────── mTLS ───────▶│ Envoy Sidecar   │
│ (port 15001)    │   (encrypted +     │ (port 15001)    │
│                 │    authenticated)  │                 │
└─────────────────┘                    └─────────────────┘
```

### Istio Traffic Policies Per Namespace

```yaml
# Applied globally — strict mTLS everywhere
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system
spec:
  mtls:
    mode: STRICT     # No plaintext service-to-service traffic allowed

---
# Circuit Breaker + Retry (per service via DestinationRule)
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: auth-service-dr
  namespace: shared-core
spec:
  host: auth-service
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        h2UpgradePolicy: UPGRADE
        http2MaxRequests: 1000
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 10s
      baseEjectionTime: 30s   # Circuit break for 30s after 5 errors
    retries:
      attempts: 3
      perTryTimeout: 5s
      retryOn: "5xx,reset,connect-failure"
```

### Istio Observability
Istio automatically generates:
- **Metrics** → exported to Prometheus (request count, latency, error rate per service pair)
- **Traces** → Zipkin-format spans exported to Tempo
- **Access logs** → Envoy access logs exported to Loki

---

## 6. Async Event Architecture (Kafka)

### Kafka Deployment Strategy

```
MSK Cluster: cypherx-kafka
├── Brokers: 3 (multi-AZ, one per AZ)
├── Replication factor: 3
├── Min in-sync replicas: 2
└── Encryption: TLS + at-rest encryption (AWS KMS)

Schema Registry: Confluent Schema Registry (self-hosted in K8s)
└── Enforces Avro/Protobuf schemas for all events
└── Prevents breaking schema changes from reaching consumers
```

### Topic Design

```
Topic Naming Convention: cypherx.<domain>.<entity>.<event-type>
                                                     (created/updated/deleted/requested/completed)

Core Platform Topics:
┌─────────────────────────────────────────────────────────────────────────────┐
│ Topic Name                              │ Partitions │ Retention │ Producer  │
├─────────────────────────────────────────┼────────────┼───────────┼───────────┤
│ cypherx.auth.agent.registered           │ 6          │ 7 days    │ auth-svc  │
│ cypherx.auth.agent.deactivated          │ 6          │ 7 days    │ auth-svc  │
│ cypherx.auth.credential.rotated         │ 6          │ 7 days    │ auth-svc  │
│ cypherx.llms.request.completed          │ 12         │ 3 days    │ llms-gw   │
│ cypherx.llms.budget.alert               │ 3          │ 7 days    │ llms-gw   │
│ cypherx.guardrails.violation.detected   │ 12         │ 30 days   │ guard-svc │
│ cypherx.memory.memory.stored            │ 6          │ 7 days    │ mem-svc   │
│ cypherx.rag.ingestion.completed         │ 6          │ 7 days    │ rag-svc   │
│ cypherx.rag.ingestion.failed            │ 6          │ 30 days   │ rag-svc   │
│ cypherx.agent.task.submitted            │ 24         │ 3 days    │ xagent    │
│ cypherx.agent.task.completed            │ 24         │ 3 days    │ xagent    │
│ cypherx.agent.task.failed               │ 24         │ 30 days   │ xagent    │
│ cypherx.agent.a2a.delegated             │ 12         │ 3 days    │ xagent    │
│ cypherx.platform.audit.event            │ 12         │ 90 days   │ all svcs  │
│ cypherx.billing.usage.recorded          │ 6          │ 7 days    │ all svcs  │
│ cypherx.platform.alert.fired            │ 3          │ 7 days    │ platform  │
└─────────────────────────────────────────────────────────────────────────────┘

Dead Letter Topics (DLQ — all failed consumer events land here):
└── cypherx.dlq.<original-topic-name>   (retain 30 days, manual replay)
```

### Consumer Group Pattern

```
Each service that consumes Kafka events has a dedicated consumer group:
  Consumer Group: cypherx-<service-name>-<purpose>

Example:
  cypherx-platform-mgmt-audit-ingester
  cypherx-billing-usage-aggregator
  cypherx-llms-budget-tracker
```

### Event Schema Standard

```json
{
  "event_id": "uuid-v4",
  "event_type": "cypherx.agent.task.completed",
  "schema_version": "1.0.0",
  "produced_at": "2026-05-22T10:00:00Z",
  "trace_id": "uuid-v4",
  "tenant_id": "org-uuid",
  "producer_service": "xagent",
  "payload": { ... }
}
```

---

## 7. Data Layer Architecture

### PostgreSQL Strategy

```
AWS RDS PostgreSQL Setup:
├── Primary instance:   db.r6g.xlarge (multi-AZ, auto-failover)
├── Read replica:       db.r6g.large  (for read-heavy queries)
├── Storage:            gp3, 100GB, autoscale to 1TB
├── Backup:             Daily automated, 7-day retention, point-in-time recovery
└── Encryption:         AWS KMS at rest, TLS in transit

Connection Pooling (PgBouncer — deployed in K8s ns: data):
  ├── Transaction mode pooling (default for all services)
  ├── Pool size: 20 per service
  └── Max client connections: 500
```

### Database-per-Service Isolation

Each SharedCore service gets its own **PostgreSQL schema** (not a separate instance — saves cost, maintains isolation):

```
PostgreSQL Database: cypherx_platform
│
├── schema: auth         (agent identities, API keys, policies)
├── schema: llms         (provider configs, usage records, cost data)
├── schema: guardrails   (policies, violations, audit log)
├── schema: memory       (memory records, metadata — vector data in vector store)
├── schema: rag          (knowledge bases, documents, ingestion jobs)
├── schema: xagent       (agent definitions, task records, workflow state)
├── schema: platform     (service registry, deployments, config)
└── schema: px0_bridge   (integration event log with px0)

Isolation rule: Service X can ONLY access schema X (enforced via PostgreSQL roles).
Cross-service data access = via REST API or Kafka event, never via shared schema.
```

### Valkey (Redis OSS fork) Strategy

```
Valkey Cluster (AWS ElastiCache, Valkey engine):
├── 3-node cluster (multi-AZ)
├── Cluster mode: enabled (16 hash slots distributed)
└── Encryption: TLS + at-rest

Usage per purpose:
┌─────────────────────────────────────────────────────────────────┐
│ Purpose              │ Key Pattern                │ TTL         │
├──────────────────────┼────────────────────────────┼─────────────┤
│ Rate limiting        │ rl:{tenant_id}:{route}     │ 60s window  │
│ Session cache        │ sess:{session_id}          │ 24h         │
│ LLM response cache   │ llm-cache:{hash}           │ 1h (config) │
│ JWT blacklist        │ jwt-revoked:{jti}          │ TTL = exp   │
│ Agent capability     │ agent-caps:{agent_id}      │ 5min        │
│ Skill index cache    │ skill-idx:{query-hash}     │ 10min       │
│ Tool health cache    │ tool-health:{tool_name}    │ 30s         │
│ Idempotency keys     │ idem:{idempotency-key}     │ 24h         │
└─────────────────────────────────────────────────────────────────┘
```

### Vector Store Strategy (for Memory & RAG)

Both SharedCore/Memory and SharedCore/RAG need a vector database. Pluggable via the same cloud abstraction principle:

```
Current choice: pgvector (PostgreSQL extension)
  └── Runs inside the same RDS instance (no extra infra for Phase 0–3)
  └── Upgrade path: Qdrant or Weaviate as a managed service when scale demands it

Vector Store Interface:
  IVectorStore
  ├── upsert(id, vector, metadata)
  ├── query(vector, topK, filter)     → []ScoredResult
  ├── delete(id)
  └── deleteByFilter(filter)

Current implementation: pgvector adapter
Future implementations: Qdrant adapter / Weaviate adapter (swap via config, zero service code change)
```

---

## 8. Observability Architecture

### The Four Pillars

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        OBSERVABILITY STACK                                   │
│                                                                               │
│  Metrics ──────► Prometheus ──────────────────► Grafana (Dashboards)        │
│  (every /metrics endpoint)                       (alerts, charts)            │
│                                                                               │
│  Logs ──────────► Promtail (DaemonSet) ─────────► Loki ──────► Grafana     │
│  (stdout/stderr    (collects from all pods)       (log store)  (LogQL)      │
│   in JSON format)                                                             │
│                                                                               │
│  Traces ────────► Istio Envoy sidecars ──────────► Tempo ──────► Grafana   │
│  (W3C TraceContext  (auto-instruments all          (trace store) (TraceQL)  │
│   headers)          service calls)                                           │
│                                                                               │
│  Alerts ────────► Alertmanager ──────────────────► PagerDuty / Slack        │
│  (Prometheus        (routing rules)                                          │
│   rules)                                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Mandatory Observability Contract

**Every service in the platform MUST implement:**

```
GET /health        → 200 OK if alive  (liveness probe)
GET /ready         → 200 OK if ready to accept traffic  (readiness probe)
GET /metrics       → Prometheus exposition format

Standard Prometheus metrics (every service exports these):
  http_requests_total{method, route, status_code}
  http_request_duration_seconds{method, route, quantile}
  http_request_size_bytes
  http_response_size_bytes

Service-specific additional metrics are optional but encouraged.
```

### Structured Log Format

**Every service MUST emit logs in this JSON format:**

```json
{
  "timestamp": "2026-05-22T10:00:00.000Z",
  "level": "INFO",
  "service": "llms-gateway",
  "version": "1.2.3",
  "trace_id": "abc123",
  "span_id": "def456",
  "tenant_id": "org-uuid",
  "agent_id": "agent-uuid",
  "request_id": "req-uuid",
  "message": "LLM request completed",
  "duration_ms": 342,
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "input_tokens": 1200,
  "output_tokens": 450
}
```

### Trace Propagation Standard

All services **MUST** propagate W3C TraceContext headers:
- `traceparent: 00-{trace-id}-{span-id}-{flags}`
- `tracestate: cypherx={tenant_id}`

Istio injects these automatically for all inter-service calls. Services only need to forward headers on outbound calls.

### Grafana Dashboard Structure

```
Grafana
├── Folder: Platform Overview
│   ├── Platform Health (all services up/down, SLA)
│   └── Platform Cost (LLM spend, compute cost, Kafka lag)
│
├── Folder: SharedCore
│   ├── Auth: request rate, auth failures, JWT issuance rate
│   ├── LLMs: requests/sec, latency by provider, token usage, cost/hr
│   ├── Guardrails: violations/min, rule hit rates, latency
│   ├── Memory: read/write ops, vector search latency, storage size
│   └── RAG: ingestion rate, query latency, hit rate, index size
│
├── Folder: xAgent
│   ├── Task throughput, task latency, error rate
│   ├── A2A call graph, delegation depth
│   └── Orchestration: workflow completion rate, step durations
│
├── Folder: Infrastructure
│   ├── K8s node CPU/Memory per node group
│   ├── Kafka: lag per consumer group, throughput per topic
│   ├── PostgreSQL: query time, connections, replication lag
│   └── Valkey: hit rate, memory usage, eviction rate
│
└── Folder: Tenant Analytics (per-tenant SaaS metrics)
    ├── Request volume per tenant
    ├── Cost per tenant
    └── Error rate per tenant
```

---

## 9. Secret & Config Architecture

### Current: Doppler

```
Doppler (SaaS Secret Manager)
│
├── Project: cypherx-platform
│   ├── Environment: dev      → syncs to K8s ns secrets (dev cluster)
│   ├── Environment: staging  → syncs to K8s ns secrets (staging)
│   └── Environment: prod     → syncs to K8s ns secrets (prod)
│
└── Sync mechanism: Doppler Kubernetes Operator
    └── Watches DopplerSecret CRD → creates/updates K8s Secret objects
    └── Auto-rotates: Doppler pushes updated values; K8s Secrets update; pods restart

Example DopplerSecret CRD:
  apiVersion: secrets.doppler.com/v1alpha1
  kind: DopplerSecret
  metadata:
    name: llms-gateway-secrets
    namespace: shared-core
  spec:
    tokenSecret:
      name: doppler-token-secret  # Doppler service token stored as K8s secret
    managedSecret:
      name: llms-gateway-env      # Output K8s secret name
      namespace: shared-core
```

### Future Migration Path: HashiCorp Vault

```
Phase: Post-MVP hardening (Phase 8)
Migration steps:
  1. Deploy Vault on K8s (Vault Helm chart) or Vault Cloud
  2. Enable Vault Agent Injector for K8s
  3. Migrate Doppler secrets to Vault KV store
  4. Replace DopplerSecret CRDs with Vault Agent annotations
  5. No service code changes — secrets still arrive as K8s env vars
  6. Vault adds: dynamic DB credentials, PKI engine, lease-based rotation

Abstraction maintained: services read secrets from K8s env vars.
The source of those env vars changes (Doppler → Vault), not the service code.
```

### Config Management Pattern

Non-sensitive configuration (feature flags, tuning params) lives in **ConfigMaps**:

```
ConfigMap naming: <service-name>-config
  ├── Contains: non-sensitive tuning (rate limits, timeouts, model aliases)
  └── Hot-reload: services watch ConfigMap via K8s watch API or reload on pod restart

Sensitive config (API keys, DB passwords): Doppler → K8s Secret → env var injection
```

---

## 10. CI/CD Pipeline Architecture

### GitOps Model

```
Developer
  │  git push feature-branch
  ▼
GitHub Repository
  │
  ├── GitHub Actions: CI Pipeline (triggered on PR)
  │   ├── Step 1: lint + format check
  │   ├── Step 2: unit tests
  │   ├── Step 3: integration tests (spin up test containers)
  │   ├── Step 4: security scan (Trivy for container, Snyk for deps)
  │   ├── Step 5: build Docker image
  │   ├── Step 6: push to ECR with tag: <service>:<git-sha>
  │   └── Step 7: update image tag in gitops-config repo (opens PR)
  │
  ├── GitHub Actions: CD Gate (triggered on merge to main)
  │   ├── Full test suite
  │   ├── Build + push image with semver tag
  │   └── Auto-update gitops-config/envs/staging/
  │
  └── gitops-config/ (separate repo — source of truth for K8s state)
      ├── envs/
      │   ├── dev/        ← ArgoCD watches this
      │   ├── staging/    ← ArgoCD watches this
      │   └── prod/       ← ArgoCD watches this (manual sync gate)
      └── services/
          ├── auth/
          │   └── values.yaml (image.tag: abc123)
          ├── llms/
          └── ...

ArgoCD (ns: argocd)
  ├── App: cypherx-dev     → gitops-config/envs/dev/     → dev cluster
  ├── App: cypherx-staging → gitops-config/envs/staging/ → staging cluster
  └── App: cypherx-prod    → gitops-config/envs/prod/    → prod cluster
      └── Sync policy: MANUAL (requires approval before prod deploy)
```

### GitHub Actions Workflow Structure

```
.github/workflows/
├── ci.yml              ← Runs on every PR (lint, test, scan, build)
├── cd-staging.yml      ← Runs on merge to main (deploy to staging)
├── cd-prod.yml         ← Runs on release tag (deploy to prod, manual approval)
├── security-scan.yml   ← Weekly full security audit (Trivy, SAST)
└── schema-validate.yml ← Validates OpenAPI specs, Kafka schemas, K8s manifests
```

### Container Image Standards

```
Image naming: <ECR_REGISTRY>/cypherx/<service-name>:<tag>

Tags used:
  ├── <git-sha>           ← Every build (immutable, used in gitops-config)
  ├── <semver>            ← On release (e.g., 1.2.3)
  ├── staging             ← Mutable tag pointing to latest staging build
  └── latest              ← Mutable tag pointing to latest prod release (only on prod deploy)

Multi-stage Dockerfile standard:
  Stage 1: build    (compiler/deps image — large, not shipped)
  Stage 2: runtime  (distroless or alpine — minimal attack surface)
```

---

## 11. Request & Data Flow Diagrams

### Flow A: External API Request (Happy Path)

```
External Client
  │  POST /v1/tasks  { JWT: Bearer <token> }
  ▼
Route53 → ALB
  │  SSL termination
  ▼
Kong Gateway
  │  1. JWT Plugin: extract token, verify signature against px0 public key
  │  2. Rate Limit Plugin: check Valkey counter for this consumer
  │  3. Inject X-Request-ID, X-Trace-ID headers
  │  4. Route to xagent namespace
  ▼
Istio Ingress Gateway
  │  mTLS handshake with target pod's Envoy sidecar
  ▼
xAgent Pod (Envoy sidecar → app container)
  │  1. Verify JWT claims (agent_id, tenant_id, scopes) with SharedCore/Auth
  │  2. Read relevant memories from SharedCore/Memory (async, parallel)
  │  3. Check input with SharedCore/Guardrails
  │  4. Query relevant skills from SharedCore/RAG
  │  5. Build enriched prompt
  │  6. Call SharedCore/LLMs /chat/completions
  │  7. Check output with SharedCore/Guardrails
  │  8. Write significant memories to SharedCore/Memory (async)
  │  9. Publish cypherx.agent.task.completed to Kafka (async)
  │  10. Return 200 response
  ▼
Client receives response

Side effects (async, non-blocking):
  ├── Kafka event: billing service consumes usage data
  ├── Kafka event: platform management consumes audit log event
  └── Memory stored in PostgreSQL (background job)
```

### Flow B: Service-to-Service (via Istio mTLS)

```
SharedCore/LLMs calls SharedCore/Auth for credential validation:

llms-gateway (app) → llms-gateway (Envoy sidecar)
  │  HTTP/2 with trace headers (traceparent, X-Tenant-ID)
  ▼
  Istio mTLS tunnel
  (SPIFFE identity: spiffe://cluster.local/ns/shared-core/sa/llms-gateway)
  ▼
auth-service (Envoy sidecar) → auth-service (app)
  │  Envoy enforces AuthorizationPolicy:
  │    allow: principals from shared-core namespace only
  ▼
Response → back through mTLS tunnel → llms-gateway
```

### Flow C: Async Event Processing

```
LLMs Gateway completes a request
  │
  ▼
Kafka Producer (in llms-gateway)
  │  Topic: cypherx.llms.request.completed
  │  Payload: { tenant_id, agent_id, model, tokens_in, tokens_out, cost_usd, trace_id }
  ▼
Kafka Broker (MSK, 3 nodes)
  │  Replicated across 3 brokers
  ▼  ▼  ▼
Multiple consumer groups read independently:

Consumer: cypherx-billing-usage-aggregator
  → Aggregates token usage per tenant → PostgreSQL billing schema
  → Publishes billing events to px0 via px0-bridge service

Consumer: cypherx-platform-mgmt-audit-ingester
  → Writes audit log entry → PostgreSQL platform schema

Consumer: cypherx-llms-budget-tracker
  → Checks against tenant budget → if >80% consumed, publishes budget alert event
```

### Flow D: A2A Agent Communication

```
Orchestrator Agent (ns: xagent)
  │  1. Receives high-level task via POST /v1/workflows
  │  2. Decomposes into subtasks using LLM
  │  3. Discovers specialist agents via Agent Registry (Valkey cache + PostgreSQL)
  │  4. Issues A2A task messages (Kafka: cypherx.agent.a2a.delegated)
  │
  ├──► Specialist Agent A (ns: xagent)
  │    ├── Receives task via A2A endpoint
  │    ├── Validates sender JWT (SharedCore/Auth)
  │    ├── Executes task (MCP tool calls, LLM calls)
  │    └── Returns result to Orchestrator via callback_url
  │
  └──► Specialist Agent B (ns: xagent, parallel)
       ├── Same flow as Agent A
       └── Returns result independently
  │
  ▼
Orchestrator collects both results
  │  Synthesis call to SharedCore/LLMs
  ▼
Final response returned to original caller
```

### Flow E: Secret Injection at Pod Startup

```
Terraform provisions EKS cluster
  │
  ▼
ArgoCD syncs Helm chart with DopplerSecret CRD
  │
  ▼
Doppler Operator detects new DopplerSecret
  │  Fetches secrets from Doppler SaaS API
  │  Creates K8s Secret object in correct namespace
  ▼
Pod starts: K8s injects Secret as env vars
  │
  ▼
Service reads env vars (DATABASE_URL, API_KEYS, etc.)
  No service code knows about Doppler — just reads env vars.

On secret rotation:
  Doppler pushes updated value → Doppler Operator updates K8s Secret
  → Pod restart triggered (via Reloader or rolling restart policy)
```

---

## 12. Namespace & Service Layout

### Complete Service Inventory (Phase 0–5)

```
ns: shared-core
├── auth-service          (Deployment, Service, HPA, PDB, ServiceAccount)
├── llms-gateway          (Deployment, Service, HPA, PDB, ServiceAccount)
├── guardrails-service    (Deployment, Service, HPA, PDB, ServiceAccount)
├── memory-service        (Deployment, Service, HPA, PDB, ServiceAccount)
└── rag-service           (Deployment, Service, HPA, PDB, ServiceAccount)

ns: xagent
├── agent-runtime         (Deployment, Service, HPA, PDB, ServiceAccount)
├── orchestrator          (Deployment, Service, HPA, PDB, ServiceAccount)
└── a2a-router            (Service + Ingress rule only — routes A2A to correct agent)

ns: tools
├── tool-web-search       (Deployment, Service, HPA)
├── tool-code-exec        (Deployment, Service, HPA — special: gVisor sandbox)
├── tool-http-client      (Deployment, Service, HPA)
└── tool-file-ops         (Deployment, Service, HPA — special: per-pod volume)

ns: platform-mgmt
└── platform-service      (Deployment, Service, HPA, PDB, ServiceAccount)

ns: ingress
├── kong                  (Deployment, Service type:LoadBalancer, HPA)
└── istio-ingressgateway  (Deployment, Service, HPA — managed by Istio)

ns: data
├── pgbouncer             (Deployment, Service — routes to RDS external)
└── valkey                (StatefulSet — or points to ElastiCache endpoint)

ns: observability
├── prometheus            (StatefulSet, Service, PVC)
├── alertmanager          (Deployment, Service)
├── grafana               (Deployment, Service, PVC)
├── loki                  (StatefulSet, Service, PVC)
├── tempo                 (StatefulSet, Service, PVC)
└── promtail              (DaemonSet — runs on every node)

ns: argocd
└── argocd                (standard ArgoCD install)

ns: px0-bridge
└── px0-bridge-service    (Deployment — handles event translation between platforms)
```

### Istio Authorization Policy Model

```
Default: DENY ALL inter-namespace traffic unless explicitly allowed.

Explicit allow rules (AuthorizationPolicy):

xagent → shared-core:     ALLOW (all xagent service accounts)
xagent → tools:           ALLOW (all xagent service accounts)
tools  → shared-core:     ALLOW (auth only — to validate JWTs)
shared-core → shared-core: ALLOW (services can call each other)
kong   → shared-core:     ALLOW (ingress routing)
kong   → xagent:          ALLOW (ingress routing)
kong   → platform-mgmt:   ALLOW (ingress routing)
observability → *:         ALLOW (scraping — read-only ports only)
px0-bridge → shared-core: ALLOW (auth validation only)

NOT ALLOWED (architecturally enforced):
tools  → xagent:          DENY  (tools never initiate calls to agent runtime)
tools  → tools:           DENY  (tools never call each other)
data   → *:               DENY  (data layer never initiates outbound calls)
```

---

## 13. IaC Repository Structure

```
cypherx-infra/                   ← Terraform + Terragrunt monorepo
│
├── modules/                     ← Reusable Terraform modules (cloud-agnostic wrappers)
│   ├── eks-cluster/             ← EKS cluster module (wraps AWS EKS + nodegroups)
│   ├── kafka/                   ← MSK module (wraps AWS MSK)
│   ├── postgresql/              ← RDS module (wraps AWS RDS PostgreSQL)
│   ├── valkey/                  ← ElastiCache module (Valkey engine)
│   ├── s3-bucket/               ← S3 bucket module
│   ├── ecr-repo/                ← ECR repository module
│   ├── vpc/                     ← VPC + subnets + security groups
│   └── dns/                     ← Route53 + ACM certificates
│
├── environments/                ← Terragrunt configs per environment
│   ├── terragrunt.hcl           ← Root config (remote state, provider versions)
│   ├── dev/
│   │   ├── terragrunt.hcl
│   │   ├── eks/
│   │   ├── kafka/
│   │   ├── postgresql/
│   │   └── valkey/
│   ├── staging/
│   │   └── (same structure as dev, different variable values)
│   └── prod/
│       └── (same structure as dev, different variable values)
│
├── k8s-addons/                  ← Terraform manages K8s add-ons (Helm releases)
│   ├── istio/
│   ├── kong/
│   ├── argocd/
│   ├── cert-manager/
│   ├── prometheus-stack/
│   ├── loki-stack/
│   ├── tempo/
│   └── doppler-operator/
│
└── scripts/
    ├── bootstrap.sh             ← First-time cluster bootstrap
    └── rotate-certs.sh          ← Manual cert rotation helper

cypherx-gitops/                  ← ArgoCD GitOps repo (separate from app code)
│
├── envs/
│   ├── dev/
│   │   ├── shared-core/
│   │   │   ├── auth-values.yaml
│   │   │   ├── llms-values.yaml
│   │   │   └── ...
│   │   ├── xagent/
│   │   └── tools/
│   ├── staging/
│   └── prod/
│
└── base/                        ← Base Helm values (shared across envs)
    ├── auth-base-values.yaml
    └── ...
```

---

## 14. Platform Foundation Setup Order

This is the precise sequence to set up the platform **before any service code is written**. Each step must complete before the next begins.

```
PHASE 0 — INFRASTRUCTURE FOUNDATION
─────────────────────────────────────────────────────────────────────────────
Step 1:  Git repositories
         ├── Create: cypherx-platform (monorepo for all service code)
         ├── Create: cypherx-infra    (Terraform + Terragrunt)
         └── Create: cypherx-gitops   (ArgoCD sync source)

Step 2:  AWS Account & IAM Setup
         ├── Create dedicated AWS account for CypherX AI (separate from px0)
         ├── Enable AWS Organizations
         ├── IAM roles: GitHubActionsRole (OIDC, least-privilege)
         ├── IAM roles: TerraformRole (for infra provisioning)
         └── Enable AWS CloudTrail + AWS Config for audit

Step 3:  Terraform Remote State
         ├── S3 bucket: cypherx-terraform-state (versioned, encrypted)
         └── DynamoDB table: cypherx-terraform-locks

Step 4:  VPC & Networking (Terraform)
         ├── VPC: 10.0.0.0/16
         ├── Private subnets: 3 AZs (10.0.1.0/24, 10.0.2.0/24, 10.0.3.0/24)
         ├── Public subnets: 3 AZs (for ALB, NAT gateways)
         ├── NAT gateways: 1 per AZ
         └── Security groups: base rules

Step 5:  EKS Cluster (Terraform)
         ├── EKS control plane (managed)
         ├── Node groups (system, core-services, agent-runtime, tools, observability)
         ├── EKS OIDC provider (for IAM Roles for Service Accounts)
         └── kubectl access configured

Step 6:  Supporting AWS Services (Terraform)
         ├── MSK cluster (Kafka, 3 brokers)
         ├── RDS PostgreSQL (primary + read replica, multi-AZ)
         ├── ElastiCache (Valkey engine, 3 nodes)
         ├── ECR repositories (one per service)
         └── S3 buckets (assets, RAG ingestion uploads)

Step 7:  K8s Add-ons Installation (Terraform + Helm)
         ├── cert-manager  (TLS certificate management)
         ├── Istio (istiod, ingress gateway, egress gateway)
         ├── Kong Gateway (+ configure routes in declarative YAML)
         ├── ArgoCD (+ configure app-of-apps pointing to cypherx-gitops)
         ├── Doppler Operator (+ connect to Doppler project)
         └── AWS Load Balancer Controller (for ALB integration)

Step 8:  Observability Stack (ArgoCD + Helm)
         ├── kube-prometheus-stack (Prometheus + Alertmanager + Grafana)
         ├── Loki + Promtail (DaemonSet on all nodes)
         └── Tempo (distributed trace store)

Step 9:  Namespaces & Baseline Policies
         ├── Create all namespaces (from Section 12)
         ├── Enable Istio injection on all app namespaces
         ├── Apply NetworkPolicies (default deny + explicit allows)
         └── Apply Istio AuthorizationPolicies (DENY ALL + explicit allows)

Step 10: Contract & Standards Publication
         ├── Publish API error format schema to contracts/
         ├── Publish A2A message schema
         ├── Publish MCP tool manifest schema
         ├── Publish Skill definition schema
         ├── Publish JWT claims structure
         ├── Publish standard log JSON format
         └── Publish OpenAPI base template for all services

Step 11: GitHub Actions Base Workflows
         ├── ci.yml (lint, test, scan, build, push to ECR)
         ├── cd-staging.yml (update gitops-config on merge to main)
         └── cd-prod.yml (manual gate, update gitops-config on release tag)

─────────────────────────────────────────────────────────────────────────────
✓ FOUNDATION COMPLETE — Services can now be built and deployed
─────────────────────────────────────────────────────────────────────────────
```

### Foundation Health Checklist (Before Any Service Development)

```
□ EKS cluster is running, all node groups healthy
□ kubectl access confirmed for all team members (RBAC configured)
□ Namespaces created with correct Istio injection labels
□ Istio control plane running, mTLS STRICT mode enabled
□ Kong Gateway running, routes configured, reachable via ALB
□ ArgoCD running, connected to cypherx-gitops repo
□ Doppler Operator running, secrets syncing to K8s namespaces
□ Prometheus scraping K8s metrics (node, pod, container)
□ Grafana accessible, base K8s dashboards imported
□ Loki collecting logs from all system pods
□ Tempo receiving traces from Istio
□ MSK Kafka accessible from within cluster (test producer/consumer confirmed)
□ RDS PostgreSQL accessible from data namespace (PgBouncer test confirmed)
□ Valkey accessible from data namespace (redis-cli ping confirmed)
□ ECR repositories created for all planned services
□ GitHub Actions CI pipeline runs on a test service (lint → build → push)
□ ArgoCD auto-syncs gitops-config changes to dev namespace
□ All contract schemas published and reviewed by team
```

---

## Cross-Cutting Architecture Invariants

These rules apply **everywhere, always, with no exceptions:**

| Invariant | Rule | Enforcement |
|-----------|------|-------------|
| **All traffic encrypted** | TLS at edge (ALB/Kong), mTLS internally (Istio) | Istio PeerAuthentication: STRICT |
| **All requests authenticated** | No unauthenticated endpoint anywhere | Kong JWT plugin + Istio AuthorizationPolicy |
| **All services stateless** | No in-process state; all state in PostgreSQL/Valkey/Kafka | Code review + architecture review |
| **Tenant isolation** | `tenant_id` in every DB query, every API call, every Kafka event | Schema-level PostgreSQL roles + JWT claim enforcement |
| **Observable by default** | /health, /ready, /metrics mandatory on every service | Readiness/liveness probe config required in Helm chart |
| **Trace everything** | `trace_id` propagated through every service call, every Kafka event | W3C TraceContext + Istio auto-injection |
| **Version all APIs** | All routes prefixed /v1/, /v2/ — never remove a version | Kong route config + API schema versioning |
| **Secrets never in code** | All secrets via Doppler → K8s Secrets → env vars | Doppler operator; CI scan rejects hardcoded secrets |
| **Cloud abstraction** | Services never call AWS SDK directly | Provider Interface pattern; code review |
| **Async for non-real-time** | Operations >200ms or non-critical use Kafka | Architecture review on each service design |

---

*End of CypherX AI Enterprise Architecture Flow Plan*
*This document governs the platform foundation. Individual service architecture is defined separately.*
*Review and update this document when: changing a technology decision, adding a new infrastructure layer, or onboarding a new cloud provider.*
