# k8s-addons — Kubernetes Add-ons (Terraform-managed Helm releases)

Each add-on is a self-contained Terraform module (`main.tf`, `variables.tf`,
`outputs.tf`, `versions.tf`, `README.md`) that wraps a pinned Helm release plus
any required custom resources. Targets **Terraform >= 1.9**, `helm ~> 2.13`,
`kubernetes ~> 2.31`, `kubectl (gavinbunney) ~> 1.14`.

## Group G4 — ingress / mesh / dns / certs

| Add-on | Component | Summary |
|--------|-----------|---------|
| [`istio/`](./istio) | 7, 8 | Istio 1.22.x: istio-base + istiod + gateway; global STRICT mTLS; `otel-tempo` OTLP→Tempo; `mesh-tracing` Telemetry (W3C, 100 dev/10 prod); metrics-permissive PeerAuth (15020+9090); per-host `tls.mode: DISABLE` DestinationRules |
| [`kong/`](./kong) | 8 | Kong 3.6.x DB-less; LoadBalancer→ALB (ACM, 443/80, ssl-redirect); base plugins (correlation-id, request-id, response-transformer); `/v1/*` route map |
| [`cert-manager/`](./cert-manager) | 9 | cert-manager + `letsencrypt-prod` ClusterIssuer, scoped to internal dashboards only |
| [`aws-lbc/`](./aws-lbc) | 10 | AWS Load Balancer Controller with IRSA |
| [`external-dns/`](./external-dns) | 17b | external-dns (Route53, IRSA), watches Ingress/Service |
| [`metrics-server/`](./metrics-server) | 17b | metrics-server (HPA prerequisite) |
| [`reloader/`](./reloader) | 17b | stakater/reloader (rolls pods on Secret/ConfigMap change) |

## Group G5 — gitops / secrets / observability / autoscale + namespaces / netpol

| Add-on | Component | Summary |
|--------|-----------|---------|
| [`namespaces/`](./namespaces) | 6 | `kubernetes_namespace` for the 10 CypherX namespaces with exact `istio-injection` labels (`istio-system` owned by G4 istio addon) |
| [`network-policies/`](./network-policies) | 6, 7 | Default deny-all-ingress + same-namespace allow per ns; placeholder explicit-allows (observability scrape, argocd deploy) behind `enable_explicit_allows` |
| [`argocd/`](./argocd) | 12 | ArgoCD 2.11.x; cypherx-gitops repo reg; App-of-Apps root → `apps/dev-apps.yaml`; automated sync dev/staging + manual prod |
| [`doppler-operator/`](./doppler-operator) | 11 | Doppler operator; per-namespace bootstrap token Secrets (from G3 doppler provider); reference `auth-service-secrets` DopplerSecret |
| [`kube-prometheus-stack/`](./kube-prometheus-stack) | 13 | Prometheus (50Gi PVC) + Grafana (10Gi PVC) + Alertmanager + node-exporter + kube-state-metrics + 6 pre-imported dashboards |
| [`loki/`](./loki) | 13 | Loki on S3 (`cypherx-loki-logs-<env>`), 30d retention, per-tenant ingestion 10/20 MB |
| [`promtail/`](./promtail) | 13 | DaemonSet, JSON parsing, low-cardinality labels ONLY (OOM guard) |
| [`tempo/`](./tempo) | 13 | `tempo-distributed` on S3 (`cypherx-tempo-traces-<env>`), 7d retention, OTLP gRPC 4317 + HTTP 4318 + Zipkin fallback |
| [`karpenter/`](./karpenter) | 4, 17b | Karpenter v1.x + EC2NodeClass + NodePools (core/agent/tools). No system/observability NodePools (managed-NG non-overlap guard) |

### G5 load-bearing invariants (do NOT "fix")

- **Karpenter vs managed-nodegroup non-overlap** (karpenter): only `core`/`agent`/
  `tools` NodePools; `system` + `observability` stay managed NGs. The two scalers
  would fight otherwise.
- **Do NOT consolidate observability** (karpenter + kube-prometheus-stack + loki +
  tempo): no observability NodePool; the stateful observability stack is pinned to
  the fixed observability managed NG (`nodeSelector: node-role=observability`).
- **Loki low-cardinality labels** (promtail): `tenant_id`/`agent_id`/`request_id`/
  `trace_id`/`span_id` are JSON fields queried via `| json`, NEVER stream labels —
  promoting them creates 20k+ streams/service and OOMs Loki.
- **ArgoCD sync policy** (argocd): automated self-heal+prune in dev/staging,
  **manual** in prod.

> Other add-ons in `k8s-addons/` (kong, istio, cert-manager, aws-lbc, external-dns,
> metrics-server, reloader, pgbouncer) are owned by other groups — not modified here.

## Load-bearing invariants (do NOT "fix")

These are encoded verbatim in the relevant `main.tf`/README and are intentional:

- **ALB→Kong plaintext boundary** (kong): ALB terminates TLS (ACM); Kong receives
  plain HTTP inside the VPC. Kong→backend is mTLS via Istio. Removing this breaks
  the deploy.
- **`/v1/agents/*` → Auth, not xAgent** (kong): Auth owns agent identity. Routing
  it to xagent breaks the Contract 15 smoke test and every JWT mint call.
- **Global PeerAuthentication stays STRICT** (istio): mTLS exceptions are scoped
  to specific ports (15020/9090 for scrape) or specific hosts (`tls.mode: DISABLE`
  for non-mesh destinations like PgBouncer) — never by weakening the global policy.
- **Tracing is OTLP→Tempo, NOT Zipkin, NOT openCensusAgent** (istio); propagation
  is W3C Trace Context (`traceparent` + `tracestate`) per Contract 8.
- **cert-manager scope** is internal dashboards only — ALB certs are ACM, mesh
  certs are the Istio CA.

## Conventions

- All chart versions are **pinned**.
- **No hardcoded secrets.** ACM ARNs / IRSA role ARNs / zone IDs / cluster names
  are env-varying **inputs** sourced from the dns, eks, vpc, and `modules/iam`
  stacks (or SSM). ACME contact email is operational, not a secret.
- IAM is never created here (separation of duty — IAM lives in
  `environments/<env>/iam/`). Add-ons consume IRSA role ARNs by variable.
- Provider configuration (kube context / cluster auth) is supplied by the calling
  Terragrunt stack, not hardcoded in these modules.
