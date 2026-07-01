# k8s-addons/istio — Istio Service Mesh (Components 7 & 8)

Terraform-managed Helm releases for the Istio service mesh plus the mesh-policy
custom resources. Targets **Istio 1.22.x** (`var.istio_version`, default `1.22.3`).

## What this module installs

| Resource | Helm chart / CR | Purpose |
|----------|-----------------|---------|
| `istio-base` | `base` (1.22.x) | CRDs + cluster roles |
| `istiod` | `istiod` (1.22.x) | Control plane; carries `meshConfig` |
| `istio-ingressgateway` | `gateway` (1.22.x) | Ingress gateway in the `ingress` namespace |
| `default` PeerAuthentication | CR | Global **STRICT** mTLS (Component 7) |
| `mesh-tracing` Telemetry | CR | Binds `otel-tempo`, sets sampling (Component 7) |
| `metrics-permissive` PeerAuthentication | CR | PERMISSIVE on ports 15020 + 9090 |
| `*-no-mtls` DestinationRule(s) | CR | `tls.mode: DISABLE` for non-mesh hosts |

## Tracing — OTLP to Tempo (Component 7 + Contract 8)

The mesh exports traces to Tempo over **OTLP gRPC** via the `otel-tempo`
extension provider. This is the mandatory configuration path:

1. `meshConfig.extensionProviders` declares `otel-tempo` pointing at
   `tempo-distributor.observability.svc.cluster.local:4317`.
2. A mesh-wide `Telemetry` resource (`mesh-tracing`, namespace `istio-system`)
   binds that provider and sets `randomSamplingPercentage`.

- **NOT Zipkin** (Tempo's Zipkin receiver is a legacy fallback only — Component 13).
- **NOT** the deprecated `openCensusAgent` (explicitly forbidden by Component 7).
- Propagation is **W3C Trace Context** (`traceparent` + `tracestate`) per
  **Contract 8** — Istio injects/forwards these headers; `tracestate` carries
  `cypherx={tenant_id}`.

### Sampling

| env | `randomSamplingPercentage` |
|-----|----------------------------|
| dev | 100.0 |
| staging | 100.0 (mirrors dev) |
| prod | 10.0 |

Derived from `var.env`; override with `var.tracing_sample_percentage`.

## mTLS posture

- **Global: STRICT** (`PeerAuthentication/default` in `istio-system`).
- **Exception 1 — metrics scrape (REQUIRED):** `PeerAuthentication/metrics-permissive`
  sets PERMISSIVE on ports **15020** (Istio merged metrics) and **9090** (the app
  `/metrics` convention). The sidecar-less `observability` namespace can then
  scrape over plain HTTP. Everything outside these ports stays STRICT.
- **Exception 2 — non-mesh destinations (REQUIRED):** a `DestinationRule` with
  `trafficPolicy.tls.mode: DISABLE` per host the mesh calls that has no sidecar.
  The `data` namespace (PgBouncer/Valkey) runs without injection and uses its own
  TLS; a sidecar'd caller under global STRICT would otherwise fail TLS
  negotiation. Default entry: `pgbouncer.data.svc.cluster.local`.

  > **Repeat for other non-mesh hosts.** Add entries to `var.non_mesh_hosts` for
  > any other host in `data` or any non-mesh service the mesh must reach (e.g.
  > RDS/MSK endpoint references resolved by ExternalName Services). Do **NOT**
  > weaken the global PeerAuthentication — keep every exception scoped to the
  > specific host.

## Sidecar injection

Injection is enabled per-namespace via the `istio-injection: enabled` label on
`shared-core`, `xagent`, `tools`, `platform-mgmt`, `ingress`, and `px0-bridge`
(set by the namespaces stack, Component 6 — not by this module). `data`,
`observability`, and `argocd` are intentionally **not** injected.

## Access logs

`meshConfig.accessLogFile = /dev/stdout` — sidecar access logs go to stdout and
are collected by Promtail (Component 7/13).

## Inputs

| Variable | Default | Notes |
|----------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` (drives sampling) |
| `istio_version` | `1.22.3` | Pinned 1.22.x |
| `istio_namespace` | `istio-system` | Control plane |
| `gateway_namespace` | `ingress` | Ingress gateway |
| `tempo_otlp_grpc_endpoint` | `tempo-distributor.observability.svc.cluster.local` | Component 13 |
| `tempo_otlp_grpc_port` | `4317` | OTLP gRPC |
| `tracing_sample_percentage` | `null` (derived) | Override sampling |
| `metrics_permissive_ports` | `[15020, 9090]` | Scrape exception ports |
| `non_mesh_hosts` | `{ pgbouncer = ... }` | DestinationRule DISABLE hosts |

## Secrets

None. Istio certificates are issued by the Istio CA (Component 9 note: Istio
certs are NOT managed by cert-manager). No secret values are hardcoded.

## Providers

`helm ~> 2.13`, `kubernetes ~> 2.31`, `kubectl (gavinbunney) ~> 1.14`. The
provider configs (kube context / cluster auth) are supplied by the calling stack.
