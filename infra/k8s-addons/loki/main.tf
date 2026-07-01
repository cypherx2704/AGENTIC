# ─────────────────────────────────────────────────────────────────────────────
# Component 13 — Loki
#
#   Mode:      single-binary (dev), microservices/scalable (prod)
#   Storage:   S3 bucket  cypherx-loki-logs-<env>
#   Retention: 30 days
#   Per-tenant rate limit: ingestion_rate_mb=10, ingestion_burst_size_mb=20 per service
#
#   S3 bucket itself is provisioned by the G3 s3-bucket module; this module
#   references it by name and uses the IRSA role (irsa_role_arn) for access.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  s3_bucket   = var.s3_bucket_name != "" ? var.s3_bucket_name : "cypherx-loki-logs-${var.environment}"
  single_node = var.environment == "dev"

  # Service account annotations for IRSA (S3 access) when a role ARN is provided.
  sa_annotations = var.irsa_role_arn != "" ? {
    "eks.amazonaws.com/role-arn" = var.irsa_role_arn
  } : {}
}

resource "helm_release" "loki" {
  name             = "loki"
  namespace        = var.namespace
  create_namespace = var.create_namespace

  repository = "https://grafana.github.io/helm-charts"
  chart      = "loki"
  version    = var.chart_version

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  values = [
    yamlencode({
      # SingleBinary (dev) vs scalable read/write/backend (prod).
      deploymentMode = local.single_node ? "SingleBinary" : "SimpleScalable"

      loki = {
        auth_enabled = true # multi-tenant: tenant via X-Scope-OrgID

        # ── S3 object storage (Component 13) ──────────────────────────────────
        storage = {
          type = "s3"
          bucketNames = {
            chunks = local.s3_bucket
            ruler  = local.s3_bucket
            admin  = local.s3_bucket
          }
          s3 = {
            region = var.aws_region
            # No static keys — access via IRSA (irsa_role_arn on the SA).
          }
        }

        schemaConfig = {
          configs = [
            {
              from         = "2024-04-01"
              store        = "tsdb"
              object_store = "s3"
              schema       = "v13"
              index = {
                prefix = "loki_index_"
                period = "24h"
              }
            },
          ]
        }

        # ── Limits: 30d retention + per-tenant ingestion rate limits ───────────
        limits_config = {
          retention_period = var.retention_period # 720h = 30d

          # Per-tenant ingestion rate/burst (Component 13).
          ingestion_rate_mb       = var.ingestion_rate_mb       # 10
          ingestion_burst_size_mb = var.ingestion_burst_size_mb # 20

          # Reject queries/streams that would explode cardinality — defence in
          # depth alongside Promtail's low-cardinality label set.
          max_label_names_per_series  = 15
          reject_old_samples          = true
          reject_old_samples_max_age  = "168h"
          max_global_streams_per_user = 5000
          per_stream_rate_limit       = "5MB"
          per_stream_rate_limit_burst = "20MB"
        }

        # Compactor enforces retention deletion against S3.
        compactor = {
          retention_enabled    = true
          delete_request_store = "s3"
        }

        limits_config_note = "tenant_id is X-Scope-OrgID at the Loki API; it is NOT a stream label."
      }

      serviceAccount = {
        create      = true
        annotations = local.sa_annotations
      }

      # ── Topology: single-binary in dev, scalable in prod ────────────────────
      singleBinary = {
        replicas     = local.single_node ? 1 : 0
        nodeSelector = var.node_selector
        persistence = {
          enabled      = true
          storageClass = "gp3"
          size         = "20Gi"
        }
      }

      read = {
        replicas     = local.single_node ? 0 : 3
        nodeSelector = var.node_selector
      }
      write = {
        replicas     = local.single_node ? 0 : 3
        nodeSelector = var.node_selector
        persistence = {
          enabled      = true
          storageClass = "gp3"
          size         = "20Gi"
        }
      }
      backend = {
        replicas     = local.single_node ? 0 : 3
        nodeSelector = var.node_selector
        persistence = {
          enabled      = true
          storageClass = "gp3"
          size         = "20Gi"
        }
      }

      # No self-monitoring stack — Prometheus (kube-prometheus-stack) scrapes Loki.
      monitoring = {
        selfMonitoring = { enabled = false }
        lokiCanary     = { enabled = false }
        serviceMonitor = { enabled = true }
      }
      test    = { enabled = false }
      gateway = { nodeSelector = var.node_selector }
    }),
    var.extra_values,
  ]
}
