# k8s-addons/metrics-server — metrics-server (Component 17b)

Terraform-managed Helm release for metrics-server.

- **Chart:** `metrics-server/metrics-server` (`var.chart_version`, default `3.12.1`)
- **Namespace:** `kube-system`
- **Purpose:** HPA prerequisite — supplies pod CPU/memory metrics to the
  Horizontal Pod Autoscaler. Without it, all HPAs sit at "unknown" and never
  scale (Component 17b).

## Scheduling

Pinned to the `system` managed node group (`node-role=system`) with a
`CriticalAddonsOnly` toleration (Component 4).

## Inputs

| Variable | Default | Notes |
|----------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` |
| `chart_version` | `3.12.1` | Pinned |
| `namespace` | `kube-system` | |
| `replicas` | `1` | Set 2 for HA in prod |

## Secrets

None.
