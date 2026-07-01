# ─────────────────────────────────────────────────────────────────────────────
# Component 13 — Observability Stack (kube-prometheus-stack)
#
#   Components: Prometheus + Alertmanager + Grafana + Node Exporter +
#               kube-state-metrics
#   Storage:    Prometheus — 50GB PVC (gp3); Grafana — 10GB PVC
#
#   Pinned onto the fixed "observability" managed node group (Component 4): the
#   Prometheus/Loki PVCs are pinned and Karpenter consolidation breaks EBS attach,
#   so this stack does NOT run on Karpenter-managed nodes.
#
#   Grafana dashboards pre-imported (Component 13):
#     - Kubernetes cluster overview
#     - Node resource usage
#     - Kafka lag (kafka-exporter)
#     - PostgreSQL stats (postgres-exporter)
#     - Kong metrics
#     - Istio service mesh
# ─────────────────────────────────────────────────────────────────────────────

locals {
  grafana_host = var.grafana_host != "" ? var.grafana_host : "grafana.${var.environment}.cypherx.ai"

  # Pre-imported dashboards. grafana.com dashboard IDs sourced via the chart's
  # `dashboards` provider (gnetId). Loaded by Grafana's dashboard sidecar.
  preimported_dashboards = {
    # Kubernetes cluster overview
    k8s-cluster-overview = { gnetId = 7249, revision = 1, datasource = "Prometheus" }
    # Node resource usage (node-exporter full)
    node-resource-usage = { gnetId = 1860, revision = 37, datasource = "Prometheus" }
    # Kafka lag (kafka-exporter / Kafka overview)
    kafka-lag = { gnetId = 7589, revision = 5, datasource = "Prometheus" }
    # PostgreSQL stats (postgres-exporter)
    postgresql-stats = { gnetId = 9628, revision = 7, datasource = "Prometheus" }
    # Kong metrics
    kong-metrics = { gnetId = 7424, revision = 13, datasource = "Prometheus" }
    # Istio service mesh
    istio-mesh = { gnetId = 7639, revision = 158, datasource = "Prometheus" }
  }
}

resource "helm_release" "kube_prometheus_stack" {
  name             = "kube-prometheus-stack"
  namespace        = var.namespace
  create_namespace = var.create_namespace

  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.chart_version

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  values = [
    yamlencode({
      # ── Prometheus ──────────────────────────────────────────────────────────
      prometheus = {
        prometheusSpec = {
          retention = var.prometheus_retention
          # Pin to the observability managed NG (PVC affinity, Component 4).
          nodeSelector = var.node_selector
          # 50GB gp3 PVC (Component 13).
          storageSpec = {
            volumeClaimTemplate = {
              spec = {
                storageClassName = var.storage_class
                accessModes      = ["ReadWriteOnce"]
                resources = {
                  requests = {
                    storage = var.prometheus_pvc_size
                  }
                }
              }
            }
          }
          # Scrape app /metrics across the mesh — Component 7 grants a PERMISSIVE
          # mTLS exception on ports 15020 + 9090 so this no-sidecar namespace can
          # scrape over plain HTTP. Discover all ServiceMonitors/PodMonitors.
          serviceMonitorSelectorNilUsesHelmValues = false
          podMonitorSelectorNilUsesHelmValues     = false
          ruleSelectorNilUsesHelmValues           = false
        }
      }

      # ── Alertmanager ────────────────────────────────────────────────────────
      alertmanager = {
        alertmanagerSpec = {
          nodeSelector = var.node_selector
          storage = {
            volumeClaimTemplate = {
              spec = {
                storageClassName = var.storage_class
                accessModes      = ["ReadWriteOnce"]
                resources = {
                  requests = {
                    storage = "5Gi"
                  }
                }
              }
            }
          }
        }
      }

      # ── Grafana ─────────────────────────────────────────────────────────────
      # adminPassword from Doppler (never hardcoded). When empty, the key is
      # OMITTED (not set to null) so the chart auto-generates one into the
      # grafana Secret — a literal null would override the chart default.
      grafana = merge(var.grafana_admin_password != "" ? { adminPassword = var.grafana_admin_password } : {}, {
        # 10GB persistent PVC (Component 13).
        persistence = {
          enabled          = true
          type             = "pvc"
          storageClassName = var.storage_class
          accessModes      = ["ReadWriteOnce"]
          size             = var.grafana_pvc_size
        }
        nodeSelector = var.node_selector

        # Internal ALB / VPN-only (Component 5).
        "grafana.ini" = {
          server = {
            root_url            = "https://${local.grafana_host}"
            serve_from_sub_path = false
          }
        }

        # Pre-import dashboards via the sidecar (Component 13 list).
        dashboardProviders = {
          "dashboardproviders.yaml" = {
            apiVersion = 1
            providers = [
              {
                name            = "cypherx-default"
                orgId           = 1
                folder          = "CypherX"
                type            = "file"
                disableDeletion = false
                editable        = true
                options = {
                  path = "/var/lib/grafana/dashboards/cypherx-default"
                }
              },
            ]
          }
        }
        dashboards = {
          cypherx-default = {
            for name, d in local.preimported_dashboards :
            name => {
              gnetId     = d.gnetId
              revision   = d.revision
              datasource = d.datasource
            }
          }
        }

        # Loki + Tempo datasources wired here so traces/logs link from dashboards.
        additionalDataSources = [
          {
            name      = "Loki"
            type      = "loki"
            access    = "proxy"
            url       = "http://loki-gateway.${var.namespace}.svc.cluster.local"
            isDefault = false
          },
          {
            name      = "Tempo"
            type      = "tempo"
            access    = "proxy"
            url       = "http://tempo-query-frontend.${var.namespace}.svc.cluster.local:3100"
            isDefault = false
          },
        ]
      })

      # ── node-exporter + kube-state-metrics ──────────────────────────────────
      nodeExporter = {
        enabled = true
      }
      kubeStateMetrics = {
        enabled = true
      }
      "kube-state-metrics" = {
        nodeSelector = var.node_selector
      }
    }),
    var.extra_values,
  ]
}
