# Phase 1 — Infrastructure Foundation
> **Status:** ⏳ Pending | **Depends On:** Phase 0 | **Blocks:** Phase 2–14
> **First Cycle:** ⚡ Partial — core infra required; full observability and GitOps can come after first cycle

---

## Phase Overview

Phase 1 provisions and configures the **entire platform infrastructure layer** before any service code is written. Services are built on top of this foundation — if it changes after services are running, it's painful. Getting this right first is non-negotiable.

This phase uses **Terraform + Terragrunt** (IaC) and installs all Kubernetes add-ons. At the end of this phase, a healthy empty cluster exists with networking, security, observability, and GitOps fully operational — ready to receive service deployments.

**Deliverable:** A running, secure, observable Kubernetes platform with all supporting infrastructure (Kafka, PostgreSQL, Valkey, Kong, Istio, ArgoCD, Doppler, observability stack) operational across dev and staging environments.

---

## High Level Design

### Infrastructure Layers

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         CLOUD PROVIDER LAYER (AWS)                           │
│  Route53  ·  ACM  ·  ALB  ·  EKS  ·  MSK  ·  RDS  ·  ElastiCache  ·  ECR  │
│  VPC  ·  Subnets  ·  Security Groups  ·  IAM  ·  KMS  ·  CloudTrail         │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │ Terraform provisions
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                      KUBERNETES CLUSTER (EKS)                                │
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ system-nodes │  │ core-services│  │ agent-runtime│  │ observability  │  │
│  │ (3 On-Demand)│  │ (3–10 auto)  │  │ (2–20 auto)  │  │ (2 On-Demand)  │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  └────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │ Helm/K8s applies
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                       KUBERNETES ADD-ONS LAYER                               │
│                                                                              │
│  Kong (API Gateway)  ·  Istio (Service Mesh)  ·  cert-manager (TLS)         │
│  AWS Load Balancer Controller  ·  ArgoCD (GitOps)  ·  Doppler Operator      │
│  Prometheus + Grafana + Loki + Tempo  ·  Promtail (DaemonSet)               │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │ Ready to receive services
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│               SUPPORTING DATA & MESSAGING INFRASTRUCTURE                     │
│                                                                              │
│  PostgreSQL (RDS, multi-AZ)  ·  PgBouncer (connection pooler)               │
│  Valkey (ElastiCache, 3-node cluster)                                        │
│  Kafka (MSK, 3-broker, multi-AZ)  ·  Schema Registry                        │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Terraform Repository Layout

```
cypherx-infra/
├── modules/                    ← Cloud-agnostic wrapper modules
│   ├── vpc/
│   ├── eks-cluster/
│   ├── kafka/
│   ├── postgresql/
│   ├── valkey/
│   ├── ecr-repo/
│   ├── s3-bucket/
│   └── dns/
│
├── environments/
│   ├── terragrunt.hcl          ← Root: remote state config, provider versions
│   ├── dev/
│   │   ├── terragrunt.hcl
│   │   ├── vpc/terragrunt.hcl
│   │   ├── eks/terragrunt.hcl
│   │   ├── kafka/terragrunt.hcl
│   │   ├── postgresql/terragrunt.hcl
│   │   └── valkey/terragrunt.hcl
│   ├── staging/
│   │   └── (mirrors dev)
│   └── prod/
│       └── (mirrors dev, larger sizes)
│
└── k8s-addons/                 ← Terraform manages Helm releases
    ├── istio/
    ├── kong/
    ├── argocd/
    ├── cert-manager/
    ├── prometheus-stack/
    ├── loki/
    ├── tempo/
    └── doppler-operator/
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ **First Cycle** items must be operational before Phase 2 begins.
> 📋 **Full Enterprise** items should be designed now, implemented after first cycle verification.

---

### Component 1 — AWS Account & IAM ⚡

**What it is:** The AWS account foundation. All resources live here.

**Resources to provision:**
```
AWS Account: cypherx-ai (separate from px0 account)

IAM Roles:
  GitHubActionsRole
    ├── Trust: GitHub OIDC (sts:AssumeRoleWithWebIdentity)
    ├── Permissions: ECR push, S3 read (terraform state), EKS describe
    └── Boundary: cannot create IAM resources

  TerraformRole
    ├── Trust: CI/CD and local developer (with MFA)
    ├── Permissions: Create/modify VPC, EKS, RDS, MSK, ElastiCache, ECR, Route53
    └── Boundary: cannot modify IAM roles (separation of duty)

  EKS Node Role (IRSA base)
    └── EC2 can assume, scoped to ECR pull only

  EKS Service Account Roles (IRSA per-service — provisioned in Phase 2+)
    └── Least-privilege per service (e.g., S3 access for RAG service only)

AWS Services to enable:
  CloudTrail (all API calls logged, 1-year retention)
  AWS Config (resource configuration history)
  GuardDuty (threat detection)
```

> **Note on the IAM separation-of-duty boundary:**
> The original spec said "TerraformRole cannot modify IAM" — but Terraform must create per-service IRSA roles in Phase 2+. To resolve this:
> - **`TerraformInfraRole`** — provisions VPC, EKS, RDS, MSK, ElastiCache, ECR, Route53. CANNOT create or modify IAM roles. This is the default role used by CI for infra Terraform.
> - **`TerraformIAMRole`** — provisions only IAM roles, IRSA mappings, and policy attachments. Used by a separate, smaller Terraform stack (`environments/<env>/iam/`). PRs against this stack require a second human approver in CI (CODEOWNERS rule).
> - Neither role can modify itself, the GitHubActionsRole, or any role tagged `protected=true` (root/admin roles).

---

### Component 2 — Terraform Remote State ⚡

```
S3 Bucket: cypherx-terraform-state-<account-id>
  ├── Versioning: enabled
  ├── Encryption: SSE-KMS (aws/s3 key)
  ├── Public access: blocked
  └── Lifecycle: noncurrent versions expire after 90 days

DynamoDB Table: cypherx-terraform-locks
  ├── Billing mode: PAY_PER_REQUEST
  └── Hash key: LockID (String)
```

---

### Component 3 — VPC & Networking ⚡

```
VPC: 10.0.0.0/16  (region: us-east-1)

Subnets:
  Private (EKS nodes, RDS, MSK, ElastiCache):
    10.0.1.0/24  (us-east-1a)
    10.0.2.0/24  (us-east-1b)
    10.0.3.0/24  (us-east-1c)

  Public (ALB, NAT Gateways only):
    10.0.101.0/24 (us-east-1a)
    10.0.102.0/24 (us-east-1b)
    10.0.103.0/24 (us-east-1c)

NAT Gateways: 1 per AZ (3 total — HA for outbound traffic from private subnets)

Internet Gateway: 1 (for public subnets)

Route Tables:
  Public RT:  0.0.0.0/0 → Internet Gateway
  Private RT: 0.0.0.0/0 → NAT Gateway (per-AZ)

Security Groups:
  sg-alb:          Inbound 443 from 0.0.0.0/0; Outbound to sg-kong
  sg-kong:         Inbound from sg-alb; Outbound to sg-eks-nodes
  sg-eks-nodes:    Inbound from sg-kong + sg-eks-nodes; Outbound 443 (AWS APIs)
  sg-rds:          Inbound 5432 from sg-eks-nodes only
  sg-valkey:       Inbound 6379 from sg-eks-nodes only
  sg-kafka:        Inbound 9092,9094 from sg-eks-nodes only
```

---

### Component 4 — EKS Cluster ⚡

> **One cluster per environment.** `cypherx-dev`, `cypherx-staging`, `cypherx-prod` are separate EKS clusters in separate AWS accounts (or at minimum, separate VPCs). Sharing a cluster across environments creates a blast radius problem: one bad CRD upgrade or one misbehaving operator takes down dev AND prod. All Terragrunt modules are environment-scoped — `environments/<env>/eks/terragrunt.hcl` produces a distinct cluster.

```
Cluster: cypherx-<env>            (one of: cypherx-dev, cypherx-staging, cypherx-prod)
  K8s version: 1.30 (latest stable at build time)
  API server access: PRIVATE ONLY (public endpoint disabled)
  Logging: api, audit, authenticator → CloudWatch Logs
  OIDC: enabled (for IRSA)
  Add-ons: kube-proxy, vpc-cni, coredns (managed by AWS)

API server access pattern:
  - CI/CD (GitHub Actions): self-hosted runners deployed INSIDE the VPC (private subnet, EKS-attached). Runners assume an IRSA role with kubectl access. No public API endpoint exposure, no IP allow-list maintenance.
  - Developers: AWS SSO + VPN (Tailscale or AWS Client VPN) → kubectl through the private endpoint.
  - Emergency break-glass: a tagged jump host in the public subnet with MFA + audited session recording. Disabled by default; enabled via a Terraform variable during incidents.
  - GitHub-hosted runner IP allow-listing is FORBIDDEN — those ranges churn and the allow-list silently rots.

Compute split (managed node groups vs Karpenter — single source of truth):
  - **EKS-managed node groups** (static, ON_DEMAND, never autoscaled by Karpenter):
      ┌────────────────┬───────────────┬─────────┬──────────┬────────────────────────────────┐
      │ Name           │ Instance type │ Size    │ AZs      │ Why managed (not Karpenter)    │
      ├────────────────┼───────────────┼─────────┼──────────┼────────────────────────────────┤
      │ system-nodes   │ t3.medium     │ 3 fixed │ 3        │ Hosts kube-system + Karpenter  │
      │                │               │         │          │ itself. Karpenter cannot       │
      │                │               │         │          │ provision its own host node.   │
      │ observability  │ m5.large      │ 2 fixed │ 2        │ Prometheus/Loki PVCs are       │
      │                │               │         │          │ pinned; consolidation breaks   │
      │                │               │         │          │ EBS attach.                    │
      └────────────────┴───────────────┴─────────┴──────────┴────────────────────────────────┘

  - **Karpenter NodePools** (dynamic, see Component 17b for the CRD spec):
      ┌────────────────┬─────────────────────────┬────────────┬─────────────────────────────┐
      │ NodePool       │ Instance shape          │ Capacity   │ Workload                    │
      ├────────────────┼─────────────────────────┼────────────┼─────────────────────────────┤
      │ core           │ c5.xlarge family        │ on-demand  │ shared-core, ingress        │
      │ agent ⚡       │ c5.2xlarge / c6i family │ on-demand  │ xagent, orchestrator        │
      │                │                         │ + spot     │ (mixed; HPA-driven)         │
      │ tools          │ c5.large / c6i family   │ on-demand  │ tools/* (Phase 7+)          │
      │                │                         │ + spot     │                             │
      └────────────────┴─────────────────────────┴────────────┴─────────────────────────────┘
      Karpenter owns provisioning AND consolidation for these NodePools; do NOT also
      create managed node groups for `core`, `agent`, or `tools` — the two scalers will
      fight (managed-NG ASG adds a node, Karpenter consolidates it minutes later, repeat).

Node labels (used for pod scheduling — applied by both managed NGs and Karpenter NodePools):
  system-nodes:    node-role=system
  core (NodePool):           node-role=core
  agent (NodePool):          node-role=agent
  tools (NodePool):          node-role=tools
  observability:   node-role=observability

Taints (prevent non-system pods on system nodes):
  system-nodes:   CriticalAddonsOnly=true:NoSchedule
```

---

### Component 5 — Supporting AWS Services ⚡

**PostgreSQL (RDS):**
```
Engine:    PostgreSQL 16
Instance:  db.r6g.xlarge (dev: db.t3.medium)
Multi-AZ:  enabled (prod) / disabled (dev)
Storage:   100GB gp3, autoscale to 1TB
Backup:    7-day retention, daily automated, point-in-time recovery
Encryption: KMS
Subnet group: private subnets only
Parameter group:
  max_connections = 1000                       (was 500 — see budget below)
  shared_preload_libraries = pg_stat_statements              (pgvector loads as a regular extension — do NOT preload)
  log_min_duration_statement = 500             (log slow queries ≥ 500ms)
  idle_in_transaction_session_timeout = 60000  (kill idle txns after 60s — prevents RLS context leak)
```

**Connection budget (RDS ≤ 1000 conns):**
| Source | Conns | Notes |
|--------|-------|-------|
| PgBouncer × 2 replicas × 7 service users × pool_size 20 | 280 | Service runtime |
| PgBouncer × 2 × DDL users (one per service) × pool_size 5 | 70 | Migration jobs |
| Direct connections (psql from bastion, monitoring exporters, RDS Performance Insights) | ~50 | Operational |
| `reserve_pool` slack | 70 | PgBouncer `reserve_pool_size = 5` per pool |
| Headroom for 8th service (skills, Phase 8) + 9th service | ~150 | Future-proofing |
| **Total used** | **~620** | Leaves ~38% headroom |

**Valkey (ElastiCache):**
```
Engine:    valkey 7.x
Cluster:   3 nodes (prod), 1 node (dev)
Node type: cache.r6g.large (prod), cache.t3.micro (dev)
Multi-AZ:  enabled (prod)
TLS:       enabled
Auth:      AUTH token (stored in Doppler)
```

**Kafka (MSK):**
```
Brokers:   3 (one per AZ)
Instance:  kafka.m5.large (prod), kafka.t3.small (dev)
Volume:    100GB per broker (gp3)
Version:   3.6.x (latest stable)
TLS:       enabled (in-transit encryption)
At-rest:   KMS encrypted
SASL:      SCRAM-SHA-512 auth
```

**ECR Repositories (one per service):**
```
cypherx/auth-service
cypherx/llms-gateway
cypherx/guardrails-service
cypherx/memory-service
cypherx/rag-service
cypherx/xagent
cypherx/orchestrator
cypherx/platform-management
cypherx/tool-web-search
cypherx/tool-code-exec
cypherx/tool-http-client
cypherx/tool-file-ops
cypherx/px0-bridge

Extended in later phases (added per phase, not at Phase 1):
  cypherx/skills-service          (Phase 8)
  cypherx/a2a-service             (Phase 10)
  cypherx/web-frontend            (Phase 12)
```

**DNS & TLS domains (required before Phase 2 Auth deploys):**

> **Hostname convention (locked in):** all environment-specific hostnames are **env-scoped**
> (`api.<env>.cypherx.ai`, `auth.<env>.cypherx.ai`). Prod uses `api.cypherx.ai` and
> `auth.cypherx.ai` as **additional aliases** so SDK/client defaults stay stable. Dev and
> staging are NEVER reachable at the env-less hostname — that prevents dev tokens being
> accepted by a misrouted prod client.
>
> Contract 1 specifies JWT `iss: https://auth.cypherx.ai` as a stable issuer **identifier**.
> Verifiers MUST treat the `iss` claim as an opaque string and discover the JWKS via their
> environment's `https://auth.<env>.cypherx.ai/.well-known/jwks.json` (configured per-env,
> not derived from `iss`). This is recorded as a deliberate split between `iss` (identity)
> and JWKS URL (resolution). Phase 2 verifier configuration must encode this explicitly.

```
Route53 hosted zone: cypherx.ai (delegated from registrar)

Records (created by Terraform, refreshed by external-dns where applicable):
  api.<env>.cypherx.ai      → ALB (Kong, this env)
  auth.<env>.cypherx.ai     → ALB (Kong, this env) — JWKS at /.well-known/jwks.json (Contract 1)
  argocd.<env>.cypherx.ai   → Internal ALB (VPN-only access)
  grafana.<env>.cypherx.ai  → Internal ALB (VPN-only access)

  # Prod-only aliases (NOT present in dev/staging):
  api.cypherx.ai            → ALIAS → api.prod.cypherx.ai
  auth.cypherx.ai           → ALIAS → auth.prod.cypherx.ai

ACM certificates (issued + auto-renewed in us-east-1):
  *.<env>.cypherx.ai        (per-env wildcard, attached to that env's public + internal ALBs)
  cypherx.ai + *.cypherx.ai (prod only, covers the bare aliases above)
```

---

### Component 6 — Kubernetes Namespaces & Policies ⚡

```
Namespaces to create (with labels):
  ingress         istio-injection: enabled
  istio-system    (managed by Istio)
  shared-core     istio-injection: enabled
  xagent          istio-injection: enabled
  tools           istio-injection: enabled
  platform-mgmt   istio-injection: enabled
  data            istio-injection: disabled  (PgBouncer, Valkey endpoint refs)
  messaging       (no pods — just ConfigMaps with broker addresses)
  observability   istio-injection: disabled  (avoids circular dependency)
  argocd          istio-injection: disabled  (bootstrapped before Istio)
  px0-bridge      istio-injection: enabled

Default NetworkPolicy per namespace:
  Deny all ingress from other namespaces by default.
  Explicit allow rules per namespace (defined in k8s-addons/network-policies/).
```

---

### Component 7 — Istio Service Mesh ⚡

```
Install method: Helm (via Terraform)
Profile: default (istiod + ingress gateway)
Version: 1.22.x (latest stable at build time)

Configuration:
  PeerAuthentication (global):  mtls.mode = STRICT
  Tracing:
    sampling = 100% (dev), 10% (prod)
    exporter = OTLP → Tempo OTLP gRPC endpoint (NOT Zipkin — see Component 13)
    propagation = W3C Trace Context (traceparent + tracestate) — matches Contract 8
    Configuration path (mandatory — `openCensusAgent` is deprecated and MUST NOT be used):
      1. Declare the OTLP backend in `meshConfig.extensionProviders`:
           extensionProviders:
             - name: otel-tempo
               opentelemetry:
                 service: tempo-distributor.observability.svc.cluster.local
                 port: 4317              # OTLP gRPC
      2. Bind it with a `Telemetry` resource (namespace: istio-system, mesh-wide):
           apiVersion: telemetry.istio.io/v1
           kind: Telemetry
           metadata:
             name: mesh-tracing
             namespace: istio-system
           spec:
             tracing:
               - providers:
                   - name: otel-tempo
                 randomSamplingPercentage: 100.0   # 10.0 in prod
  Access logs: enabled → stdout (picked up by Promtail)

Namespace sidecar injection:
  Enabled on: shared-core, xagent, tools, platform-mgmt, ingress, px0-bridge

AuthorizationPolicies (baseline — expand per phase):
  Default: DENY all cross-namespace traffic
  Allow: observability namespace can scrape /metrics from all pods (GET only)
  Allow: argocd can deploy to all namespaces
  (Per-service rules added as each service is deployed in phases 2–9)

mTLS exception for Prometheus scraping (REQUIRED — otherwise scrape fails):
  Apply a PeerAuthentication that sets PERMISSIVE mode on the metrics port only,
  so observability namespace (no sidecar) can scrape over plain HTTP.

  apiVersion: security.istio.io/v1
  kind: PeerAuthentication
  metadata:
    name: metrics-permissive
    namespace: istio-system          # applied mesh-wide
  spec:
    portLevelMtls:
      15020:                          # Istio merged metrics port
        mode: PERMISSIVE
      9090:                           # app metrics port convention
        mode: PERMISSIVE

  Services SHOULD expose /metrics on port 9090 by convention. Anything outside
  these ports remains STRICT mTLS.

mTLS exception for non-mesh data-plane destinations (REQUIRED — otherwise PgBouncer/Valkey calls fail):
  The `data` namespace runs without sidecar injection (Postgres/Valkey have their own TLS).
  A sidecar'd caller (shared-core, xagent, …) hitting `pgbouncer.data.svc.cluster.local`
  under global STRICT mTLS will fail TLS negotiation because the destination has no sidecar.

  Apply a DestinationRule per non-mesh destination that the mesh calls:

  apiVersion: networking.istio.io/v1
  kind: DestinationRule
  metadata:
    name: pgbouncer-no-mtls
    namespace: istio-system
  spec:
    host: pgbouncer.data.svc.cluster.local
    trafficPolicy:
      tls:
        mode: DISABLE                 # sidecar will not originate mTLS to this host

  Repeat for any other host in `data` or any non-mesh service the mesh must reach
  (e.g., RDS/MSK endpoint references resolved by ExternalName Services). Do NOT
  weaken global PeerAuthentication — keep the exception scoped to the specific host.
```

---

### Component 8 — Kong API Gateway ⚡

```
Install method: Helm (kong/kong chart, via Terraform)
Mode: DB-less (declarative config via KongIngress CRDs — no separate DB)
Version: 3.6.x

Service type: LoadBalancer → provisioned as AWS ALB via AWS LBC
TLS: ALB terminates TLS (ACM cert). Kong receives HTTP from ALB on port 8000.

> **mTLS boundary (intentional):** ALB→Kong is plain HTTP inside the VPC private network. This is acceptable because (a) SG `sg-kong` only accepts traffic from `sg-alb`, (b) the path traverses only AWS-managed infra inside the VPC. Kong→backend services is mTLS via Istio (Kong runs with sidecar in `ingress` namespace). Do NOT remove this comment; future reviewers will otherwise "fix" it and break the deploy.

Base plugins (installed platform-wide):
  - correlation-id   (inject X-Request-ID on every request)
  - request-id       (unique ID per request)
  - response-transformer (inject standard response headers)

Route placeholders (routes added as services are deployed in phases 2–9):
  /v1/auth/*           → shared-core/auth-service:8080
  /v1/agents/*         → shared-core/auth-service:8080   ← Auth owns agent identity (Phase 2: register, keys, token, /revoke-all-tokens)
  /v1/tokens/*         → shared-core/auth-service:8080   ← Auth owns token revocation
  /v1/authorize        → shared-core/auth-service:8080
  /v1/service-tokens   → shared-core/auth-service:8080   ← Contract 12 service token issuance
  /v1/llms/*           → shared-core/llms-gateway:8080
  /v1/guardrails/*     → shared-core/guardrails-service:8080
  /v1/memory/*         → shared-core/memory-service:8080
  /v1/rag/*            → shared-core/rag-service:8080
  /v1/tasks/*          → xagent/agent-runtime:8080      ← xAgent owns task execution
  /v1/workflows/*      → xagent/orchestrator:8080
  /v1/platform/*       → platform-mgmt/platform-service:8080

> **Route ownership rule:** `/v1/agents/*` is Auth, not xAgent. xAgent runs agent code but does NOT
> own the agent identity resource. Mixing these (e.g., routing `/v1/agents/*` to xagent) breaks the
> Contract 15 smoke test step 1 (`POST /v1/agents`) and every JWT mint call. Do not "fix" this routing
> by moving `/v1/agents/*` to xagent.
```

---

### Component 9 — cert-manager ⚡

```
Install method: Helm (cert-manager/cert-manager)
Purpose: manage TLS certificates for internal ingress rules

ClusterIssuer: letsencrypt-prod (for any internal dashboard TLS)
ALB certs: managed by AWS ACM (not cert-manager — ACM certs auto-renew)
Istio certs: managed by Istio CA (not cert-manager)
cert-manager scope: developer-facing dashboard ingresses only
```

---

### Component 10 — AWS Load Balancer Controller ⚡

```
Install method: Helm (eks/aws-load-balancer-controller)
Purpose: watches K8s Service type:LoadBalancer → creates AWS ALB automatically

IRSA role: AWSLoadBalancerControllerRole
  Permissions: ec2:*, elasticloadbalancing:*, iam:CreateServiceLinkedRole

ALB Annotations used on Kong service:
  kubernetes.io/ingress.class: alb
  alb.ingress.kubernetes.io/scheme: internet-facing
  alb.ingress.kubernetes.io/certificate-arn: <ACM cert ARN>
  alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443},{"HTTP":80}]'
  alb.ingress.kubernetes.io/ssl-redirect: '443'
```

---

### Component 11 — Doppler Operator ⚡

```
Install method: Helm (doppler/doppler-kubernetes-operator)
Purpose: sync Doppler secrets → K8s Secret objects

Doppler Service Tokens:
  One Doppler service token per (env, namespace), scoped to that namespace's config.
  Stored as K8s Secrets — the operator bootstrap secret — provisioned by Terraform
  using the Doppler Terraform provider (NOT manual kubectl). The Doppler API token
  used by Terraform lives in Doppler itself (CI-only, rotated every 90 days).
  Runbook: docs/runbooks/doppler-token-rotation.md (📋 — owned by platform team).

  > **Initial bootstrap (one-time, per environment):** the "Doppler token lives in
  > Doppler" loop has a starting point. The very first `terragrunt apply` of the
  > `environments/<env>/doppler-bootstrap/` stack is run by a platform operator with
  > a personal Doppler CLI token exported as `DOPPLER_TOKEN` in their shell. That
  > apply (a) creates the per-env service tokens and (b) writes the long-lived
  > Terraform service token back into Doppler at `ci/doppler_api_token`. From the
  > second apply onward, CI reads `ci/doppler_api_token` from Doppler — the personal
  > token is no longer required. The personal token MUST be revoked immediately
  > after this bootstrap apply. This is the only step where a human-held secret
  > touches an environment; capture the operator name + timestamp in the env's
  > infra changelog.

DopplerSecret CRD created per service when service is deployed:
  Example (auth-service):
    managedSecret.name: auth-service-secrets
    managedSecret.namespace: shared-core
    → K8s Secret created with all auth-service env vars
```

---

### Component 12 — ArgoCD (GitOps) ⚡

```
Install method: Helm (argo/argo-cd)
Namespace: argocd
Version: 2.11.x

Repositories registered:
  cypherx-gitops (GitHub, HTTPS with deploy key)

App of Apps pattern:
  Root App: cypherx-platform → watches gitops/envs/dev/
    Child Apps (created automatically from gitops repo):
      - auth-app       → gitops/envs/dev/shared-core/auth/
      - llms-app       → gitops/envs/dev/shared-core/llms/
      - (etc. added per phase)

Sync policy (dev/staging):  Automated (self-heal + prune)
Sync policy (prod):         Manual approval required in ArgoCD UI
```

---

### Component 13 — Observability Stack ⚡

```
kube-prometheus-stack (Helm):
  Components: Prometheus + Alertmanager + Grafana + Node Exporter + kube-state-metrics
  Storage: Prometheus — 50GB PVC (gp3); Grafana — 10GB PVC

Loki (Helm, loki-stack):
  Mode: single-binary (dev), microservices (prod)
  Storage: S3 bucket (cypherx-loki-logs-<env>)
  Retention: 30 days
  Per-tenant rate limit: ingestion_rate_mb=10, ingestion_burst_size_mb=20 per service.

Promtail (DaemonSet):
  Deployed on all nodes
  Collects: all pod stdout/stderr logs
  Parses: JSON log format (Contract 6)

  Loki LABELS (low-cardinality only): namespace, pod, container, service, level, environment
  NOT labels — queryable via JSON parser:  tenant_id, agent_id, request_id, trace_id, span_id

  > Adding tenant_id as a Loki label is forbidden. With 1000 tenants × 20 pods × N
  > containers, you create 20k+ active streams per service and Loki OOMs.
  > Query pattern:  {service="xagent"} | json | tenant_id="<uuid>"

Tempo (Helm, grafana/tempo):
  Storage: S3 bucket (cypherx-tempo-traces-<env>)
  Retention: 7 days
  Receivers: OTLP gRPC (port 4317) + OTLP HTTP (port 4318) — required
             Zipkin receiver enabled as a no-cost fallback for legacy clients only
  Istio → Tempo: OTLP gRPC via the `otel-tempo` extension provider (see Component 7).
                 Distributor service: tempo-distributor.observability.svc.cluster.local:4317
  Service-level traces: emit OTLP from application SDKs (OpenTelemetry) to the same endpoint.

Grafana dashboards pre-imported:
  - Kubernetes cluster overview
  - Node resource usage
  - Kafka lag (from kafka-exporter)
  - PostgreSQL stats (from postgres-exporter)
  - Kong metrics dashboard
  - Istio service mesh dashboard
```

---

### Component 14 — PgBouncer ⚡

```
Namespace: data            (istio-injection: disabled — Postgres uses its own TLS, not mesh mTLS)
Deployment: 2 replicas (HA), each on a different AZ via topologySpreadConstraints
Image:      PgBouncer ≥ 1.21 (required for prepared-statement support — earlier versions break ORMs)
Service:    pgbouncer.data.svc.cluster.local:6432 (ClusterIP, kube-proxy round-robins across replicas)

Config:
  pool_mode:               transaction          (REQUIRED for Contract 13 RLS via SET LOCAL)
  max_client_conn:         500
  default_pool_size:       20 per (database, user)
  reserve_pool_size:       5
  reserve_pool_timeout:    3
  max_prepared_statements: 200                  (PgBouncer 1.21+ — enables ORMs in transaction mode)
  server_tls_sslmode:      require
  server_idle_timeout:     600

> ORM guidance for service authors (referenced from Contract 14):
> Most ORMs auto-prepare. With PgBouncer 1.21+ and max_prepared_statements > 0,
> this works in transaction mode without code changes. If you still hit
> "prepared statement does not exist" errors, fall back to extended-query without
> prepare (e.g., pgx with QueryExecModeExec, asyncpg with prepared_statement_cache_size=0).

Databases configured (one entry per service schema):
  auth_db   → RDS endpoint, user: auth_user,    database: cypherx_platform, schema: auth
  llms_db   → RDS endpoint, user: llms_user,    database: cypherx_platform, schema: llms
  grd_db    → RDS endpoint, user: grd_user,     database: cypherx_platform, schema: guardrails
  mem_db    → RDS endpoint, user: mem_user,     database: cypherx_platform, schema: memory
  rag_db    → RDS endpoint, user: rag_user,     database: cypherx_platform, schema: rag
  xagent_db → RDS endpoint, user: xagent_user,  database: cypherx_platform, schema: xagent
  plat_db   → RDS endpoint, user: plat_user,    database: cypherx_platform, schema: platform

DDL users (separate pool, smaller, used only by Atlas migration Jobs):
  *_ddl     → pool_size: 5 each, total ~70 conns reserved
```

---

### Component 15 — Kafka Schema Registry 📋

```
Deployment: Confluent Schema Registry (self-hosted, ns: messaging)
Purpose: enforce Avro/Protobuf schemas for all Kafka events
Compatibility: BACKWARD (new schema must be able to read old messages)

Subject naming: <topic-name>-value (e.g., cypherx.agent.task.completed-value)
```

---

### Component 16 — Database Initialisation ⚡

> **Ownership split (resolves Contract 14 ambiguity):**
> - **Terraform** (`hashicorp/postgresql` provider, runs once per env) owns: database, schemas, per-service runtime users (`*_user`), per-service DDL users (`*_ddl`), pgvector extension, grants. Idempotent.
> - **Atlas** (per service, runs as K8s Job on every deploy) owns: tables, columns, indexes, RLS policies, RLS roles WITHIN the service's own schema.
> - No service migration touches another service's schema (CI-enforced — Contract 14).

**Terraform-owned bootstrap (runs once per environment, applied via `environments/<env>/postgres-bootstrap/terragrunt.hcl`):**

```sql
-- Create main database
CREATE DATABASE cypherx_platform;

-- Enable extensions in the public schema (one-time)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Per-service schemas
CREATE SCHEMA auth;
CREATE SCHEMA llms;
CREATE SCHEMA guardrails;
CREATE SCHEMA memory;
CREATE SCHEMA rag;
CREATE SCHEMA xagent;
CREATE SCHEMA platform;

-- Runtime users (least privilege; password from Doppler db/<service>/runtime_password)
CREATE USER auth_user    WITH PASSWORD :auth_runtime_pw;
GRANT USAGE ON SCHEMA auth TO auth_user;
-- Atlas creates tables in service migration; Terraform pre-grants future tables:
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO auth_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT USAGE,SELECT ON SEQUENCES TO auth_user;
-- (repeat for each service)

-- DDL users (used by Atlas migration Jobs; password from Doppler db/<service>/ddl_password)
CREATE USER auth_ddl    WITH PASSWORD :auth_ddl_pw;
GRANT CREATE, USAGE ON SCHEMA auth TO auth_ddl;
GRANT CREATEROLE TO auth_ddl;             -- so Atlas can create the RLS role for this schema
-- (repeat for each service)
```

> Atlas then takes over for actual table/index/RLS DDL — invoked from each service's `migrations` Job at deploy time, using its `*_ddl` user. See Contract 14.

> **Known limitation — `CREATEROLE` is cluster-wide, not schema-scoped.**
> Postgres does not support scoping `CREATEROLE` to a single schema, so each `*_ddl` user can
> technically create or alter any non-superuser role in the cluster (it cannot touch superusers
> or roles created by superusers it doesn't own). Contract 14's "grant CREATEROLE on the service's
> schema only" is therefore aspirational — Postgres lacks the primitive. We mitigate by:
> (a) the `*_ddl` password lives only in Doppler at `db/<service>/ddl_password` and is mounted
>     ONLY into that service's Atlas migration Job (not the runtime pod, not other services);
> (b) the Helm chart that runs the Job uses a dedicated ServiceAccount with no other secret access;
> (c) audit log review (Component 5 CloudTrail + RDS pg_audit, Phase 13) flags any `CREATE ROLE`
>     issued by a `*_ddl` user against an unexpected role name.
> Do NOT grant `CREATEROLE` to runtime users. Do NOT share a single DDL user across services.

---

### Component 17 — Kafka Topic Bootstrap ⚡

```
Topics to create on MSK cluster bootstrap (from Contract 5).

Tool: terraform-kafka-provider (Mongey/kafka) — declarative, idempotent, drift-detected.
      Avoid kafka-topics.sh scripts — they don't reconcile drift and have no plan/apply story.
Script (one-shot fallback): infra/scripts/kafka-bootstrap-topics.sh

Core topics:
┌───────────────────────────────────────┬────────────┬─────────┬───────────────┬──────────────┐
│ Topic                                 │ Partitions │ Replica │ cleanup.policy│ retention    │
├───────────────────────────────────────┼────────────┼─────────┼───────────────┼──────────────┤
│ cypherx.auth.agent.registered         │ 6          │ 3       │ compact       │ infinite     │
│ cypherx.auth.agent.deactivated        │ 6          │ 3       │ compact       │ infinite     │
│ cypherx.llms.request.completed        │ 12         │ 3       │ delete        │ 90 days      │
│ cypherx.llms.budget.alert             │ 3          │ 3       │ delete        │ 30 days      │
│ cypherx.guardrails.violation.detected │ 12         │ 3       │ delete        │ 90 days      │
│ cypherx.agent.task.submitted          │ 24         │ 3       │ delete        │ 30 days      │
│ cypherx.agent.task.completed          │ 24         │ 3       │ delete        │ 30 days      │
│ cypherx.agent.task.failed             │ 24         │ 3       │ delete        │ 30 days      │
│ cypherx.platform.audit.event          │ 12         │ 3       │ delete        │ 365 days     │
│ cypherx.billing.usage.recorded        │ 6          │ 3       │ delete        │ 365 days     │
└───────────────────────────────────────┴────────────┴─────────┴───────────────┴──────────────┘

Common topic config:
  min.insync.replicas = 2                (writes require ≥ 2 ISR acks)
  unclean.leader.election.enable = false (no data loss on broker failure)
  compression.type = lz4

DLQ topics (created alongside each non-compact topic; Contract 5 requirement):
  cypherx.<original>.dlq                 partitions: same as original, replication: 3,
                                         cleanup.policy: delete, retention: 30 days
  Example: cypherx.agent.task.completed.dlq  (24 partitions, 30 day retention)

Compact topics (auth.*) do NOT get a DLQ — consumer failure on a compacted topic is
re-read from the latest state on next startup.

> **Compact-topic message-key rule (MANDATORY — producers must override the envelope default):**
> Contract 5 says `partition_key` defaults to `tenant_id`. For compact topics that is wrong:
> compaction keeps only the latest record per **Kafka message key**, so a `tenant_id`-keyed
> compact topic collapses to one record per tenant and loses every prior agent state.
>
> For `cypherx.auth.agent.registered` and `cypherx.auth.agent.deactivated` (and any future
> compact topic about an agent), the producer MUST set the Kafka message key to `agent_id`
> (not `tenant_id`). Envelope `partition_key` should also be set to `agent_id` for these topics
> so per-agent ordering is preserved. Add this to `contracts/kafka/topics.md` when topics
> are first authored.
```

---

### Component 17b — K8s Operational Add-ons ⚡

These are required for the cluster to function correctly under load and to support HPA, DNS, and ingress dynamics. They were implicit in the master plan; calling them out explicitly here so they do not get missed.

```
metrics-server (Helm: metrics-server/metrics-server) ⚡
  Required by HPA to read pod CPU/memory metrics.
  Without it: all HPAs sit at "unknown" and never scale.

cluster-autoscaler OR Karpenter ⚡
  Choice: Karpenter (preferred — faster, AWS-native, instance flexibility)
    Install: helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter --version v1.x
    CRDs: NodePool + EC2NodeClass (Karpenter ≥ v0.32; the old `Provisioner` CRD is deprecated).
      - EC2NodeClass: one per AMI family / instance constraint group
      - NodePool: one per node-role label (core, agent, tools, observability) referencing the EC2NodeClass
    Spot-friendly: agent + tools NodePools may include `karpenter.sh/capacity-type: [spot, on-demand]`.
    Disruption budget: do NOT consolidate observability NodePool (Prometheus PVCs).
  Fallback: cluster-autoscaler if Karpenter not approved
  Without it: HPA scales pods, no new nodes appear → pods stuck Pending.

external-dns (Helm: external-dns/external-dns) ⚡
  Watches K8s Ingress/Service annotations → creates Route53 records.
  Without it: every new ingress hostname needs a manual Route53 entry.

ingress-nginx OR Istio ingress gateway 📋
  Already covered by Istio ingress gateway in Component 7.

reloader (stakater/reloader) ⚡
  Watches ConfigMap/Secret changes → rolls Deployments that reference them.
  Without it: rotated Doppler secrets do not propagate without manual restart.

descheduler 📋
  Re-balances pods after node-group rebalancing or autoscale events.
```

---

### Component 17c — Local Development Story ⚡

Engineers must be able to bring up the SharedCore + xAgent dependency graph on a single laptop without touching AWS. Lack of a local story is the #1 cause of slow service development.

**Tooling:**
- **Tilt** (`tilt.dev`) — declarative local dev loop with live-update for the chosen language.
- Local cluster: **kind** (Kubernetes-in-Docker) — single-node cluster, ~30s spinup.
- Local dependencies via Docker Compose chart shipped under `dev/local/`:
  ```
  dev/local/
    ├── docker-compose.yml          ← postgres, valkey, kafka (Redpanda for speed), MinIO (S3-compat)
    ├── Tiltfile                    ← declares services + live-reload rules
    ├── seed/
    │   ├── postgres-init.sql       ← schemas, users, pgvector
    │   ├── kafka-topics.sh         ← bootstrap dev topics
    │   └── doppler.env.example     ← placeholder env vars (NEVER real keys)
    └── README.md
  ```
- A developer runs `tilt up` and gets the full SharedCore stack at `localhost:8080..808N` within ~2 minutes.
- **Kafka substitute:** Redpanda (Kafka API-compatible, single binary, fits in a laptop).
- **S3 substitute:** MinIO (S3 API-compatible).
- **Istio/Kong NOT included** in local — services hit each other directly via DNS (`http://auth-service:8080`).

This is ⚡ first-cycle because without it, every service team must develop against a shared dev cluster and step on each other.

---

### Component 18 — GitHub Actions Base Workflows ⚡

```
Auth model:
  - Each repo has a GitHub OIDC trust against the AWS GitHubActionsRole (Component 1).
  - No long-lived AWS access keys. CI assumes role via sts:AssumeRoleWithWebIdentity.
  - **CI secret-fetch (added for cross-cluster CI workflows like Phase 8 skills-CI):**
    GitHubActionsRole has scoped `secretsmanager:GetSecretValue` on
    `arn:aws:secretsmanager:<region>:<acct>:secret:cypherx/ci/*` only. CI workflows
    that need an in-cluster bootstrap secret (e.g., service-token mint) store that
    secret in Secrets Manager under the `cypherx/ci/<workflow>/` prefix; Doppler is
    reserved for in-cluster pods (operator-synced env vars). This is the only path
    by which GitHub Actions reaches a per-workflow secret — no long-lived Doppler
    API tokens in GH Secrets.

Image tagging convention:
  - PR builds:       <service>:pr-<pr-number>-<git-sha7>     (auto-deleted after merge/close)
  - Main commits:    <service>:sha-<git-sha7>                 (immutable, kept forever)
  - Release tags:    <service>:<semver>  (e.g., v1.2.3)       (signed, kept forever)
  - DO NOT use :latest in any deployment manifest.

.github/workflows/ci.yml
  Triggers: pull_request, push to main, push of v* tag
  Steps:
    1. Lint (golangci-lint / eslint depending on service language)
    2. Unit tests
    3. Build Docker image (multi-stage)
    4. Trivy scan (fail on CRITICAL CVEs)
    5. Push to ECR with tag per convention above
    6. (On merge to main): open PR to gitops repo with new image tag

.github/workflows/schema-validate.yml
  Triggers: pull_request (changes to contracts/)
  Steps:
    1. ajv validate all JSON schemas
    2. redocly lint openapi-base.yaml
    3. yamllint all YAML files in contracts/

GitOps cross-repo PR credentials:
  - GitHub App: cypherx-gitops-bot
      Installed on: service repos (read) + cypherx-gitops repo (contents:write, pull_requests:write)
      Private key: stored in Doppler at ci/github_app_private_key, rotated every 180 days.
  - CI exchanges the app private key for a short-lived installation token, then opens
    the gitops PR. Do NOT use a personal PAT — they tie deployments to one human.
  - All gitops PRs are auto-merged in dev/staging (ArgoCD then syncs), require human
    approval in prod (Component 12 already enforces this in ArgoCD).
```

---

### Component 19 — gitops Repository Setup ⚡

```
cypherx-gitops/
├── envs/
│   ├── dev/
│   │   └── (empty at Phase 1 end — apps added per phase)
│   ├── staging/
│   └── prod/
└── base/
    └── (Helm value files added per phase)

ArgoCD App-of-Apps structure:
  apps/dev-apps.yaml → watches envs/dev/ and creates child apps automatically
```

---

### Component 20 — Secrets Bootstrap in Doppler ⚡ (promoted from 📋)

> Promoted to first-cycle: Phase 0 Contract 12 makes service-to-service auth depend on per-service Doppler bootstrap secrets. No Auth service can run without these paths populated.

```
Doppler Project: cypherx-platform

Environments: dev, staging, prod (with promotion between them)

Path conventions (mandatory — Helm charts resolve secrets by path):

  service-auth/<service-name>/bootstrap_secret    (Contract 12 — service-to-service auth)
    Required for: auth, llms, guardrails, memory, rag, xagent, orchestrator,
                  platform-mgmt, all tools, px0-bridge, skills (Phase 8), a2a (Phase 10)

  db/<service-name>/runtime_password              (Contract 14, Component 14 — runtime DB user)
  db/<service-name>/ddl_password                  (Contract 14, Component 14 — Atlas DDL user)
    Required for every service with a Postgres schema (all SharedCore services).

  ci/github_app_private_key                       (Component 18 — gitops bot)
  ci/doppler_api_token                            (Component 11 — operator bootstrap)

Per-service config groups (existing pattern — extend per phase):
  shared-core.auth       → POSTGRES_DSN, JWT_PRIVATE_KEY, JWT_PUBLIC_KEY (JWKS material), JWT_SIGNING_KID
  shared-core.llms       → POSTGRES_DSN, ANTHROPIC_API_KEY, OPENAI_API_KEY
  shared-core.guardrails → POSTGRES_DSN
  shared-core.memory     → POSTGRES_DSN, VECTOR_STORE_URL
  shared-core.rag        → POSTGRES_DSN, VECTOR_STORE_URL, S3_BUCKET
  xagent                 → POSTGRES_DSN, AUTH_SERVICE_URL
  platform-mgmt          → POSTGRES_DSN, KAFKA_BROKERS, KAFKA_SASL_PASSWORD

All paths above populated in Doppler dev/staging BEFORE Phase 2 service development begins.
Prod paths populated before the prod cutover (Phase 13).
```

---

### Component 21 — Infrastructure Smoke Test ⚡

> Before Phase 2 starts, the *plumbing* must be proven end-to-end. The Infrastructure Health Checklist below proves components are individually up — but doesn't prove a log line written by a pod arrives in Loki, a trace ID propagates through the mesh, and a Kafka event with the Contract 5 envelope round-trips. This component closes that gap.

**Deliverable:** a single-file `echo-service` deployed to a throwaway `smoketest` namespace, exercised by an automated script, then torn down. Required gate before Phase 2 kickoff.

```
echo-service requirements:
  - Exposes GET /echo  → returns request headers as JSON, echoes one log line per Contract 6
  - Exposes /livez, /readyz, /metrics per Contract 7
  - On startup, produces ONE Kafka event to topic cypherx.smoketest.event with a
    valid Contract 5 envelope (partition_key = a fake tenant_id)
  - Sidecar injected (Istio), runs in smoketest namespace
  - Talks to PgBouncer (SELECT 1) and Valkey (PING) on startup, fails readiness if either fails
```

**Smoke-test script (`infra/scripts/infra-smoke-test.sh`) — all assertions MUST pass:**

| # | Assertion | How |
|---|-----------|-----|
| 1 | ALB → Kong → echo-service GET /echo returns 200 with `traceparent` header populated by Istio | `curl https://api.<env>.cypherx.ai/echo` |
| 2 | echo-service log line visible in Loki within 10s | `logcli query '{service="echo"}'` returns ≥ 1 result |
| 3 | Loki labels are low-cardinality only (no tenant_id, request_id, etc as labels) | `logcli labels` excludes the forbidden set |
| 4 | Trace ID from response visible in Tempo within 10s, spans cross Kong→echo | Tempo HTTP API by trace_id |
| 5 | Kafka topic `cypherx.smoketest.event` has 1 message with valid envelope JSON | `kafka-console-consumer` + jq schema validate |
| 6 | echo-service `/metrics` scraped by Prometheus (PERMISSIVE mTLS exception works) | Prom query: `up{job="echo"} == 1` |
| 7 | PgBouncer (transaction mode) accepted `BEGIN; SET LOCAL app.tenant_id=...; SELECT 1; COMMIT;` from echo-service | echo log line "rls_probe=ok" |
| 8 | Pod scaled from 1→3 via `kubectl scale`, Karpenter provisioned a node if needed, all 3 pods Ready | `kubectl get pods -n smoketest` |
| 9 | Doppler operator synced a test secret into smoketest namespace, echo-service env vars populated | echo response includes echo'd `SMOKE_SECRET_LEN` |
| 10 | After `kubectl delete ns smoketest`, no leaked Kafka topics, no orphan ALB target groups, no orphan IAM roles | sweep script |

Phase 1 cannot be marked complete until this smoke test passes against a freshly deployed dev environment, two consecutive runs.

---

## Infrastructure Health Checklist

Before Phase 2 begins, every item must be ✅:

```
Networking:
  □ VPC created, subnets in 3 AZs, NAT gateways healthy
  □ Security groups validate (no 0.0.0.0/0 on private services)

EKS:
  □ Cluster API server reachable via kubectl
  □ All node groups running minimum replicas (healthy nodes)
  □ All namespaces created with correct labels
  □ CoreDNS, kube-proxy, vpc-cni add-ons running

Service Mesh:
  □ Istiod running in istio-system (0 crash loops)
  □ PeerAuthentication STRICT mode confirmed (mtlstest tool)
  □ Istio ingress gateway running and getting external IP from ALB

API Gateway:
  □ Kong pods running (2+ replicas)
  □ ALB created and reachable on HTTPS from internet
  □ Kong admin API accessible from within cluster only
  □ Test route returns 404 (not connection error)

Data Layer:
  □ RDS PostgreSQL reachable from eks nodes (psql test)
  □ Schemas created, per-service users created
  □ pgvector extension installed
  □ PgBouncer running, proxy connection confirmed
  □ Valkey cluster reachable (redis-cli ping → PONG)
  □ MSK Kafka brokers reachable (test producer/consumer working)

Secrets:
  □ Doppler operator running (no errors)
  □ Test DopplerSecret CRD creates K8s Secret in target namespace

CI/CD:
  □ GitHub Actions CI pipeline runs on a test PR (lint → build → push to ECR → pass)
  □ ArgoCD UI accessible, connected to gitops repo, no sync errors

Observability:
  □ Prometheus scraping K8s metrics (kubectl, node exporter targets)
  □ Grafana accessible (internal or VPN), K8s dashboards loading data
  □ Loki receiving logs from Promtail (test query returns results)
  □ Tempo reachable (Istio proxy sending traces → traces visible in Grafana)
```

---

## ⚡ First Cycle Implementation Checklist

- [ ] AWS account, IAM roles (split: `TerraformInfraRole` + `TerraformIAMRole`), OIDC provider
- [ ] Terraform remote state (S3 + DynamoDB)
- [ ] VPC, subnets (3 AZs), NAT gateways, security groups
- [ ] **Per-environment EKS clusters** (`cypherx-dev`, `cypherx-staging`, `cypherx-prod`) — no shared cluster
- [ ] EKS API server: PRIVATE ONLY; self-hosted GitHub runners in VPC; VPN for developers
- [ ] Managed node groups (system-nodes, observability — fixed-size, ON_DEMAND); Karpenter NodePools (core, agent, tools — dynamic). No managed NG + Karpenter overlap on the same role.
- [ ] DNS zone `cypherx.ai` + per-env ACM wildcard certs (`*.<env>.cypherx.ai`); env-scoped `auth.<env>.cypherx.ai` + `api.<env>.cypherx.ai` records ready; prod-only bare aliases configured
- [ ] RDS PostgreSQL (dev: single-AZ) with bumped `max_connections=1000`, `idle_in_transaction_session_timeout`, `shared_preload_libraries=pg_stat_statements` (NOT vector — pgvector is a `CREATE EXTENSION`, not a preload)
- [ ] Terraform-owned DB bootstrap (databases, schemas, runtime + DDL users, pgvector) applied
- [ ] Valkey (dev: single node)
- [ ] MSK Kafka (dev: 3 brokers), core topics created with explicit retention + cleanup policy + DLQ pairings
- [ ] ECR repositories for all planned services (initial 13; extend per phase)
- [ ] All K8s namespaces with correct labels
- [ ] Istio installed, STRICT mTLS enabled; **W3C trace propagation** configured; **OTLP export to Tempo via `meshConfig.extensionProviders` + `Telemetry` API** (not Zipkin, not the deprecated `openCensusAgent`)
- [ ] PeerAuthentication exception for Prometheus scrape ports (15020, 9090) applied
- [ ] DestinationRule with `tls.mode: DISABLE` applied for non-mesh hosts the mesh calls (PgBouncer in `data` ns; any ExternalName-resolved RDS/MSK endpoints)
- [ ] Kong installed, ALB provisioned, HTTPS working; **`/v1/agents/*`, `/v1/tokens/*`, `/v1/authorize`, `/v1/service-tokens` all routed to Auth (not xAgent)**
- [ ] cert-manager, AWS LBC installed
- [ ] PgBouncer ≥ 1.21 deployed, `max_prepared_statements=200`, `reserve_pool_size=5`, ClusterIP Service
- [ ] Doppler operator installed (bootstrap via Terraform `doppler` provider), test secret sync confirmed
- [ ] **Doppler paths populated**: `service-auth/<svc>/bootstrap_secret`, `db/<svc>/runtime_password`, `db/<svc>/ddl_password`, `ci/github_app_private_key`
- [ ] ArgoCD installed, connected to gitops repo
- [ ] Prometheus + Grafana + Loki + Promtail + Tempo installed
- [ ] Loki labels restricted to low-cardinality set; `tenant_id`/`agent_id`/`request_id`/`trace_id` are JSON fields, NOT labels
- [ ] metrics-server installed (HPA prerequisite)
- [ ] Karpenter installed using **NodePool + EC2NodeClass** CRDs; provisioning new nodes on demand
- [ ] external-dns installed and creating Route53 records from K8s annotations
- [ ] reloader installed (auto-restart pods on Secret/ConfigMap change)
- [ ] Local-dev stack (`dev/local/`) — `tilt up` brings up SharedCore on laptop in <5min
- [ ] GitHub Actions CI pipeline working (test service); GitHub App `cypherx-gitops-bot` installed
- [ ] Kafka topics bootstrapped (declarative via terraform-kafka-provider); **compact `auth.agent.*` topics documented to use `agent_id` as Kafka message key (not `tenant_id`)**
- [ ] Atlas migration tooling configured (Contract 14) with reference service migration Job
- [ ] **Component 21 infra smoke test passes 2 consecutive runs** against a freshly deployed dev environment
- [ ] Infrastructure health checklist below: all ✅

## 📋 Full Enterprise Implementation Checklist

- [ ] tools node group provisioned
- [ ] Multi-AZ RDS (read replica for prod)
- [ ] Multi-node Valkey cluster (prod)
- [ ] Kafka Schema Registry deployed (first cycle uses file-based payload schemas in `contracts/kafka/events/`)
- [ ] Network policies (deny-all + explicit allow rules) applied per namespace
- [ ] Istio AuthorizationPolicies (DENY all + explicit allows) applied
- [ ] CloudTrail, GuardDuty, AWS Config enabled
- [ ] Grafana Alertmanager rules configured (latency, error rate, quota thresholds)
- [ ] Multi-region read replica for RDS (post-scaling)
- [ ] WAF rules attached to ALB (first cycle ships without WAF — flag accepted risk)
- [ ] Doppler service token rotation runbook (90-day rotation)
- [ ] All production sizing (larger instances, multi-AZ everywhere)
