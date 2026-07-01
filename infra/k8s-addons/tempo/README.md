# k8s-addons/tempo — Component 13

Installs Grafana Tempo (`grafana/tempo-distributed` chart) as the trace store.

## Config (Component 13)

| Setting | Value |
|---------|-------|
| Object storage | S3 bucket **`cypherx-tempo-traces-<env>`** |
| Retention | **7 days** (`168h`, compactor `block_retention`) |
| Receivers | **OTLP gRPC `4317` + OTLP HTTP `4318`** (required) |
| Fallback receiver | **Zipkin** (legacy clients only, no-cost) |

## Why `tempo-distributed`

Component 7 pins the Istio `otel-tempo` extension provider to
`tempo-distributor.observability.svc.cluster.local:4317`. The distributed chart
(release name `tempo`) names the distributor service **`tempo-distributor`**,
producing exactly that DNS name. The single-binary chart would not, so the
distributed chart is mandatory here.

## Trace sources

- **Istio** → OTLP gRPC via the `otel-tempo` extension provider + `Telemetry`
  resource (Component 7). W3C trace context (`traceparent`/`tracestate`,
  Contract 8). Sampling 100% dev / 10% prod (set in the Istio addon, not here).
- **Service SDKs** → OpenTelemetry OTLP to the same `:4317`/`:4318` endpoint.

## S3 access (IRSA)

Bucket provisioned by the G3 `s3-bucket` module; referenced by name. The Tempo
ServiceAccounts are annotated with `var.irsa_role_arn` — **no static AWS keys**.

## Topology

- **dev** — 1 replica per component, 10Gi gp3 ingester PVC.
- **prod** — 3× distributor/ingester, 2× querier/query-frontend, pinned to the
  observability managed NG.

## Inputs (highlights)

| Variable           | Default  | Notes |
|--------------------|----------|-------|
| `chart_version`    | `1.10.3` | tempo-distributed chart pin. |
| `s3_bucket_name`   | derived  | `cypherx-tempo-traces-<env>`. |
| `irsa_role_arn`    | `""`     | From G3 IAM. No static keys. |
| `retention_period` | `168h`   | 7d. |
