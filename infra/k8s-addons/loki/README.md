# k8s-addons/loki — Component 13

Installs Loki (Grafana `loki` chart) as the log store.

## Config (Component 13)

| Setting | Value |
|---------|-------|
| Deployment mode | `SingleBinary` (dev) / `SimpleScalable` (staging, prod) |
| Object storage | S3 bucket **`cypherx-loki-logs-<env>`** |
| Retention | **30 days** (`720h`, compactor-enforced) |
| Per-tenant ingestion rate | `ingestion_rate_mb=10` |
| Per-tenant ingestion burst | `ingestion_burst_size_mb=20` |
| Multi-tenancy | `auth_enabled=true` (tenant = `X-Scope-OrgID`) |

> **`tenant_id` is the Loki `X-Scope-OrgID`, NOT a stream label.** Combined with
> Promtail's low-cardinality label set, this prevents the stream explosion that
> would OOM Loki (see promtail README). `max_label_names_per_series=15` and
> per-stream rate limits are defence-in-depth.

## S3 access (IRSA)

The S3 bucket is provisioned by the G3 `s3-bucket` module; this module references
it by name. Loki's ServiceAccount is annotated with `var.irsa_role_arn`
(`eks.amazonaws.com/role-arn`) — **no static AWS keys**. The IRSA role (read/write
on the bucket) is provisioned by the G3 IAM stack.

## Topology

- **dev** — `SingleBinary` (1 replica), 20Gi gp3 PVC for the local WAL/cache.
- **staging/prod** — `SimpleScalable`: 3× read, 3× write, 3× backend, each pinned
  to the observability managed NG (`node-role=observability`).

## Inputs (highlights)

| Variable                  | Default | Notes |
|---------------------------|---------|-------|
| `chart_version`           | `6.6.4` | Chart pin. |
| `s3_bucket_name`          | derived | `cypherx-loki-logs-<env>`. |
| `irsa_role_arn`           | `""`    | From G3 IAM. No static keys. |
| `retention_period`        | `720h`  | 30d. |
| `ingestion_rate_mb`       | `10`    | Per-tenant. |
| `ingestion_burst_size_mb` | `20`    | Per-tenant. |
