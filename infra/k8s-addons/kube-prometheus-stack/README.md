# k8s-addons/kube-prometheus-stack — Component 13

Installs the metrics half of the observability stack via the
`prometheus-community/kube-prometheus-stack` chart.

## Components & storage (Component 13)

| Component           | Notes |
|---------------------|-------|
| Prometheus          | **50Gi gp3 PVC**, retention `15d` (long-term lives in Loki/Tempo S3) |
| Grafana             | **10Gi gp3 PVC** |
| Alertmanager        | 5Gi gp3 PVC |
| node-exporter       | enabled (DaemonSet) |
| kube-state-metrics  | enabled |

All stateful components are pinned via `nodeSelector: node-role=observability`
onto the **fixed observability managed node group** — Component 4 forbids
Karpenter consolidation here because the Prometheus PVCs are pinned and
consolidation breaks EBS attach.

## Pre-imported Grafana dashboards (Component 13)

| Dashboard | grafana.com ID |
|-----------|----------------|
| Kubernetes cluster overview | 7249 |
| Node resource usage         | 1860 |
| Kafka lag (kafka-exporter)  | 7589 |
| PostgreSQL stats (postgres-exporter) | 9628 |
| Kong metrics                | 7424 |
| Istio service mesh          | 7639 |

Loaded into the `CypherX` folder via the Grafana dashboard sidecar. Loki and
Tempo are wired as additional datasources so logs/traces cross-link from panels.

## Scrape / mTLS

`serviceMonitorSelectorNilUsesHelmValues=false` (discovers all ServiceMonitors).
The `observability` namespace runs **without** an Istio sidecar; Component 7's
`metrics-permissive` PeerAuthentication opens ports `15020` + `9090` to PERMISSIVE
mTLS so Prometheus can scrape app `/metrics` over plain HTTP.

## Secrets

`grafana_admin_password` is **sensitive** and sourced from Doppler — never
hardcoded. If empty, the chart generates a password into the `grafana` Secret.

## Inputs (highlights)

| Variable                 | Default  | Notes |
|--------------------------|----------|-------|
| `chart_version`          | `61.3.2` | Chart pin. |
| `prometheus_pvc_size`    | `50Gi`   | Component 13. |
| `grafana_pvc_size`       | `10Gi`   | Component 13. |
| `storage_class`          | `gp3`    | Component 5/13. |
| `node_selector`          | `node-role=observability` | Pin to fixed NG. |
| `grafana_admin_password` | `""`     | **sensitive**, Doppler. |
