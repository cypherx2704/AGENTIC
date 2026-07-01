# k8s-addons/namespaces — Component 6

Creates the CypherX Kubernetes namespaces with the **exact** `istio-injection`
labels mandated by Phase 1 Component 6.

## Namespaces & injection mode

| Namespace       | `istio-injection` | Why |
|-----------------|-------------------|-----|
| `ingress`       | `enabled`         | Istio ingress gateway + Kong run with sidecars |
| `shared-core`   | `enabled`         | auth/llms/guardrails/memory/rag |
| `xagent`        | `enabled`         | agent-runtime + orchestrator |
| `tools`         | `enabled`         | tool-* MCP servers (Phase 7+) |
| `platform-mgmt` | `enabled`         | platform-management service |
| `data`          | `disabled`        | PgBouncer/Valkey use their own TLS, not mesh mTLS |
| `messaging`     | *(no label)*      | No pods — ConfigMaps with broker addresses only |
| `observability` | `disabled`        | Avoids circular dependency (sidecar ↔ istiod ↔ scrape) |
| `argocd`        | `disabled`        | Bootstrapped **before** Istio |
| `px0-bridge`    | `enabled`         | px0 org/billing lifecycle bridge |

> `istio-system` is **NOT** created here. It is owned by the Istio addon (group G4)
> and created by the Istio Helm release. Creating it in two places is a conflict.

`messaging` gets **no** `istio-injection` label at all (the doc lists it without an
injection value because it runs no pods in Phase 1). All other namespaces carry an
explicit `enabled`/`disabled` value.

## Inputs

| Variable        | Type          | Default | Description |
|-----------------|---------------|---------|-------------|
| `environment`   | `string`      | —       | `dev` \| `staging` \| `prod`. Stamped as `cypherx.ai/environment`. |
| `common_labels` | `map(string)` | `{}`    | Extra labels merged onto every namespace. Cannot override istio-injection. |

## Outputs

- `namespace_names` — all created namespaces.
- `injection_enabled_namespaces` / `injection_disabled_namespaces` — used by the
  network-policies module to scope allow rules.
- `namespace_labels` — for downstream NetworkPolicy selector matching.

## Provider

Configured by the calling stack (Terragrunt-generated `provider "kubernetes"`
block pointed at the env's EKS cluster). This module pins only the provider
*version* (`hashicorp/kubernetes ~> 2.31`).
