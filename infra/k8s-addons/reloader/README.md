# k8s-addons/reloader — stakater/reloader (Component 17b)

Terraform-managed Helm release for Stakater Reloader.

- **Chart:** `stakater/reloader` (`var.chart_version`, default `1.0.121`)
- **Namespace:** `kube-system`
- **Purpose:** watches ConfigMap/Secret changes → rolls the Deployments that
  reference them. Without it, rotated Doppler secrets (Component 11) do not
  propagate to running pods without a manual restart (Component 17b).

## Behaviour

Runs with `watchGlobally: true`. Workloads opt in by annotation:

```yaml
metadata:
  annotations:
    reloader.stakater.com/auto: "true"
    # or target a specific resource:
    # secret.reloader.stakater.com/reload: "auth-service-secrets"
```

Pairs with the Doppler operator: when the operator rewrites a managed K8s Secret
after a Doppler rotation, reloader triggers a rolling restart of the annotated
Deployment so the new secret is picked up.

## Scheduling

Pinned to the `system` managed node group (`node-role=system`) with a
`CriticalAddonsOnly` toleration (Component 4).

## Inputs

| Variable | Default | Notes |
|----------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` |
| `chart_version` | `1.0.121` | Pinned |
| `namespace` | `kube-system` | |

## Secrets

None.
