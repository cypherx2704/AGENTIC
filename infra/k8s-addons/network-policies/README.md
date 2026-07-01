# k8s-addons/network-policies — Components 6 & 7

Applies the **default deny-all-ingress** NetworkPolicy baseline per namespace
(Component 6) plus **placeholder explicit-allow** policies for observability
scrape and ArgoCD deploy (Component 7).

## Policies rendered

Per guarded namespace (always):

1. **`default-deny-ingress`** — empty `podSelector` + `policyTypes: [Ingress]`
   with no ingress rules ⇒ all inbound denied. Egress is left open (pods still
   reach DNS, the API server, RDS/MSK/Valkey, external APIs).
2. **`allow-same-namespace`** — re-allows intra-namespace pod-to-pod so Istio
   sidecars and multi-pod services keep working under deny-all.

Per guarded namespace (only when `enable_explicit_allows = true`):

3. **`allow-observability-scrape`** — ingress FROM the `observability` namespace
   to ports `9090` (app metrics) and `15020` (Istio merged metrics). The L7
   "GET only" restriction is enforced by the Istio AuthorizationPolicy (G4), not
   by L3/L4 NetworkPolicy.
4. **`allow-argocd-deploy`** — ingress FROM the `argocd` namespace into the
   workload namespace.

## First-cycle posture

`enable_explicit_allows` defaults to **`false`**. Phase 1 first-cycle ships the
deny-all baseline; the explicit allows are the Component-6 "explicit allow rules
per namespace (defined in `k8s-addons/network-policies/`)" placeholders, switched
on once observability and ArgoCD are wired (Full-Enterprise checklist item
"Network policies (deny-all + explicit allow rules) applied per namespace").

## Excluded namespaces

- `istio-system` — Istio manages its own policies.
- `messaging` — no pods in Phase 1 (ConfigMaps only).

## Inputs

| Variable                  | Type           | Default | Description |
|---------------------------|----------------|---------|-------------|
| `environment`             | `string`       | —       | `dev`/`staging`/`prod`. |
| `namespaces`              | `list(string)` | C6 set  | Namespaces to guard with deny-all. |
| `observability_namespace` | `string`       | `observability` | Scrape source. |
| `argocd_namespace`        | `string`       | `argocd` | Deploy source. |
| `metrics_port`            | `number`       | `9090`  | App `/metrics` port. |
| `enable_explicit_allows`  | `bool`         | `false` | Render the allow placeholders. |

> **CNI requirement:** NetworkPolicies require a CNI that enforces them. EKS with
> the VPC CNI requires the NetworkPolicy controller enabled (or Calico). Confirm
> with the EKS cluster module (G2) before relying on these for isolation.
