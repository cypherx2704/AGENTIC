# ─────────────────────────────────────────────────────────────────────────────
# Component 13 — Tempo (grafana/tempo-distributed)
#
#   Storage:   S3 bucket  cypherx-tempo-traces-<env>
#   Retention: 7 days
#   Receivers: OTLP gRPC (4317) + OTLP HTTP (4318) — REQUIRED
#              Zipkin receiver enabled as a no-cost fallback for legacy clients
#
#   Istio -> Tempo: OTLP gRPC via the `otel-tempo` extension provider (Component 7).
#     Distributor service: tempo-distributor.observability.svc.cluster.local:4317
#     -> the distributed chart names the distributor service `tempo-distributor`,
#        matching the Component 7 meshConfig.extensionProviders entry exactly.
#   Service-level traces: app SDKs (OpenTelemetry) emit OTLP to the same endpoint.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  s3_bucket = var.s3_bucket_name != "" ? var.s3_bucket_name : "cypherx-tempo-traces-${var.environment}"

  sa_annotations = var.irsa_role_arn != "" ? {
    "eks.amazonaws.com/role-arn" = var.irsa_role_arn
  } : {}

  # dev keeps replicas low; prod scales the ingester/distributor/querier paths.
  prod = var.environment == "prod"
}

resource "helm_release" "tempo" {
  name             = "tempo"
  namespace        = var.namespace
  create_namespace = var.create_namespace

  repository = "https://grafana.github.io/helm-charts"
  chart      = "tempo-distributed"
  version    = var.chart_version

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  values = [
    yamlencode({
      # ── Trace storage on S3 (Component 13) ──────────────────────────────────
      storage = {
        trace = {
          backend = "s3"
          s3 = {
            bucket   = local.s3_bucket
            region   = var.aws_region
            endpoint = "s3.${var.aws_region}.amazonaws.com"
            # No static keys — IRSA on the SAs (serviceAccount.annotations).
          }
        }
      }

      # ── Receivers (REQUIRED: OTLP gRPC 4317 + HTTP 4318; Zipkin fallback) ────
      traces = {
        otlp = {
          grpc = {
            enabled = true # port 4317
          }
          http = {
            enabled = true # port 4318
          }
        }
        # Zipkin enabled ONLY as a no-cost fallback for legacy clients (Component 13).
        zipkin = {
          enabled = true
        }
      }

      # ── Retention: 7 days (Component 13) ────────────────────────────────────
      compactor = {
        config = {
          compaction = {
            block_retention = var.retention_period # 168h = 7d
          }
        }
      }

      serviceAccount = {
        create      = true
        annotations = local.sa_annotations
      }

      # ── Topology ────────────────────────────────────────────────────────────
      # The distributor service is named `tempo-distributor` (release name `tempo`),
      # giving tempo-distributor.observability.svc.cluster.local:4317 — the exact
      # endpoint Component 7's otel-tempo extensionProvider points at.
      distributor = {
        replicas     = local.prod ? 3 : 1
        nodeSelector = var.node_selector
      }
      ingester = {
        replicas     = local.prod ? 3 : 1
        nodeSelector = var.node_selector
        persistence = {
          enabled      = true
          storageClass = "gp3"
          size         = "10Gi"
        }
      }
      querier = {
        replicas     = local.prod ? 2 : 1
        nodeSelector = var.node_selector
      }
      queryFrontend = {
        replicas     = local.prod ? 2 : 1
        nodeSelector = var.node_selector
      }
      compactorComponent = {
        nodeSelector = var.node_selector
      }
      metricsGenerator = {
        enabled      = false
        nodeSelector = var.node_selector
      }
      memcached = {
        nodeSelector = var.node_selector
      }

      # Prometheus (kube-prometheus-stack) scrapes Tempo via ServiceMonitor.
      serviceMonitor = {
        enabled = true
      }
    }),
    var.extra_values,
  ]
}
