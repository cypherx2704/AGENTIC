# k8s-addons/kong — Kong API Gateway (Component 8)

Terraform-managed Helm release for Kong, the platform API gateway.

- **Chart:** `kong/kong` (`var.kong_chart_version`, default `2.38.0`, ships Kong **3.6.x**)
- **Mode:** DB-less (declarative config via Kong CRDs — no separate database)
- **Namespace:** `ingress` (istio-injection: enabled — Kong runs with a sidecar)
- **Service type:** `LoadBalancer` → provisioned as an **AWS ALB** via the AWS
  Load Balancer Controller (`k8s-addons/aws-lbc`)

## TLS / the ALB→Kong boundary (DO NOT "FIX")

The ALB terminates TLS with an **ACM certificate**. Kong receives **plain HTTP**
from the ALB on container port 8000 (Service port 80).

> **mTLS boundary (intentional):** ALB→Kong is plain HTTP inside the VPC private
> network. This is acceptable because (a) SG `sg-kong` only accepts traffic from
> `sg-alb`, (b) the path traverses only AWS-managed infra inside the VPC.
> Kong→backend services is mTLS via Istio (Kong runs with sidecar in the
> `ingress` namespace). **Do NOT remove this comment**; future reviewers will
> otherwise "fix" it and break the deploy.

The verbatim guard comment lives in `main.tf` above the `helm_release`.

## ALB annotations (Component 10)

Set on the Kong proxy Service:

```
kubernetes.io/ingress.class: alb
alb.ingress.kubernetes.io/scheme: internet-facing
alb.ingress.kubernetes.io/certificate-arn: <ACM cert ARN>   # var.acm_certificate_arn
alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443},{"HTTP":80}]'
alb.ingress.kubernetes.io/ssl-redirect: '443'
```

## Base plugins (platform-wide)

Installed as global `KongClusterPlugin` CRs:

| Plugin | Purpose |
|--------|---------|
| `correlation-id` | inject `X-Request-ID` on every request (Contract 8) |
| `request-id` | unique ID per request |
| `response-transformer` | inject standard response headers |

## Route map (added per phase 2–9)

Routes are declared by each service's own Helm chart when it deploys. The
authoritative path→backend mapping is encoded in `local.route_map` (and exported
as the `route_map` output):

| Path | Backend |
|------|---------|
| `/v1/auth/*` | `shared-core/auth-service:8080` |
| `/v1/agents/*` | `shared-core/auth-service:8080` — **Auth owns agent identity** |
| `/v1/tokens/*` | `shared-core/auth-service:8080` |
| `/v1/authorize` | `shared-core/auth-service:8080` |
| `/v1/service-tokens` | `shared-core/auth-service:8080` (Contract 12) |
| `/v1/llms/*` | `shared-core/llms-gateway:8080` |
| `/v1/guardrails/*` | `shared-core/guardrails-service:8080` |
| `/v1/memory/*` | `shared-core/memory-service:8080` |
| `/v1/rag/*` | `shared-core/rag-service:8080` |
| `/v1/tasks/*` | `xagent/agent-runtime:8080` — xAgent owns task execution |
| `/v1/workflows/*` | `xagent/orchestrator:8080` |
| `/v1/platform/*` | `platform-mgmt/platform-service:8080` |

> **Route ownership rule:** `/v1/agents/*` is **Auth, not xAgent**. xAgent runs
> agent code but does NOT own the agent identity resource. Mixing these (e.g.,
> routing `/v1/agents/*` to xagent) breaks the Contract 15 smoke test step 1
> (`POST /v1/agents`) and every JWT mint call. **Do not "fix" this routing** by
> moving `/v1/agents/*` to xagent. The verbatim guard lives in `main.tf`.

## Admin API

ClusterIP only — accessible from within the cluster, never via the ALB
(matches the health checklist "Kong admin API accessible from within cluster
only").

## Secrets

None hardcoded. The ACM cert ARN is an env-varying input (`var.acm_certificate_arn`),
not a secret; sourced from the dns/ACM stack output or SSM.
