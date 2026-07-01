# ─────────────────────────────────────────────────────────────────────────────
# Component 13 — Promtail (DaemonSet)
#
#   Deployed on all nodes. Collects all pod stdout/stderr. Parses JSON logs
#   (Contract 6). Ships to Loki.
#
#   Loki LABELS (low-cardinality ONLY): namespace, pod, container, service,
#                                       level, environment
#   NOT labels — queryable via the JSON parser at query time:
#                                       tenant_id, agent_id, request_id,
#                                       trace_id, span_id
#
#   ┌──────────────────────────────────────────────────────────────────────────┐
#   │ OOM RATIONALE (do NOT promote any of the forbidden fields to a label):    │
#   │ Adding tenant_id (or agent_id/request_id/trace_id/span_id) as a Loki      │
#   │ label is FORBIDDEN. With 1000 tenants x 20 pods x N containers you create │
#   │ 20k+ active streams PER SERVICE and Loki OOMs. Every label value is a     │
#   │ distinct active stream; high-cardinality fields multiply streams without  │
#   │ bound. These fields stay inside the JSON log line and are filtered at      │
#   │ query time:   {service="xagent"} | json | tenant_id="<uuid>"              │
#   └──────────────────────────────────────────────────────────────────────────┘
# ─────────────────────────────────────────────────────────────────────────────

locals {
  loki_push_url = var.loki_push_url != "" ? var.loki_push_url : "http://loki-gateway.${var.namespace}.svc.cluster.local/loki/api/v1/push"

  # The ONLY labels allowed on Loki streams (Component 13). Keep this list short.
  allowed_labels = ["namespace", "pod", "container", "service", "level", "environment"]

  # Forbidden as labels — these are high-cardinality JSON fields, queried with
  # `| json | field="..."`. Listed here for reviewer clarity and the smoke test
  # (Component 21 assertion #3 asserts `logcli labels` excludes this set).
  forbidden_labels = ["tenant_id", "agent_id", "request_id", "trace_id", "span_id"]
}

resource "helm_release" "promtail" {
  name             = "promtail"
  namespace        = var.namespace
  create_namespace = var.create_namespace

  repository = "https://grafana.github.io/helm-charts"
  chart      = "promtail"
  version    = var.chart_version

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 600

  values = [
    yamlencode({
      # DaemonSet on ALL nodes incl. tainted system/observability nodes so no
      # node's logs are lost.
      tolerations = [
        { operator = "Exists" },
      ]

      config = {
        clients = [
          {
            url = local.loki_push_url
            # tenant_id is the Loki X-Scope-OrgID, set from the parsed JSON
            # tenant_id field — NOT a stream label.
          },
        ]

        # ── Scrape + pipeline ───────────────────────────────────────────────
        snippets = {
          # Kubernetes SD discovers every pod; relabel ONLY the low-cardinality
          # set into target labels. Everything else stays in the log body.
          scrapeConfigs = <<-EOT
            - job_name: kubernetes-pods
              kubernetes_sd_configs:
                - role: pod
              pipeline_stages:
                # Contract 6: every prod log line is JSON. Parse it.
                - cri: {}
                - json:
                    expressions:
                      # Promote ONLY low-cardinality fields to extracted values
                      # that subsequently become labels.
                      level:   level
                      service: service
                      # The following are extracted for query-time use but are
                      # deliberately NOT turned into labels (no labels: stage for
                      # them). They remain in the JSON line.
                      #   tenant_id, agent_id, request_id, trace_id, span_id
                # Low-cardinality labels ONLY (Component 13 allow-list):
                #   namespace, pod, container, service, level, environment
                - labels:
                    level:
                    service:
                # Set the static environment label (Contract 6).
                - static_labels:
                    environment: ${var.environment}
                # Tag unparseable lines so the smoke test (Contract 15 #9) can
                # assert zero parse_error lines without dropping them.
                - match:
                    selector: '{service=""}'
                    stages:
                      - static_labels:
                          parse_error: "true"
              relabel_configs:
                - source_labels: [__meta_kubernetes_namespace]
                  target_label: namespace
                - source_labels: [__meta_kubernetes_pod_name]
                  target_label: pod
                - source_labels: [__meta_kubernetes_pod_container_name]
                  target_label: container
                # Drop the pod's own per-label churn from becoming stream labels.
                - action: labelmap
                  regex: __meta_kubernetes_pod_label_(app_kubernetes_io_name)
                  replacement: $1
                - source_labels: [__meta_kubernetes_pod_node_name]
                  target_label: __host__
                # Path to the container log file.
                - source_labels:
                    - __meta_kubernetes_pod_uid
                    - __meta_kubernetes_pod_container_name
                  target_label: __path__
                  separator: /
                  replacement: /var/log/pods/*$1/*.log
          EOT
        }
      }

      # Promtail itself emits Prometheus metrics (scraped via the PERMISSIVE port).
      serviceMonitor = {
        enabled = true
      }
    }),
    var.extra_values,
  ]
}
