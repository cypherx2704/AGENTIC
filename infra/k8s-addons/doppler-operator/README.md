# k8s-addons/doppler-operator — Component 11

Installs the Doppler Kubernetes Operator (Helm) and provisions the per-namespace
bootstrap **service-token Secrets** that `DopplerSecret` CRs reference.

## What it deploys

- **`helm_release.doppler_operator`** — `doppler/doppler-kubernetes-operator`
  (pinned `var.chart_version`). Syncs Doppler secrets → K8s `Secret` objects.
- **`kubernetes_secret.namespace_bootstrap`** — one Secret per `(env, namespace)`
  holding the namespace-scoped Doppler service token. **Token values come from
  `var.bootstrap_service_tokens`**, which is the output of the G3
  `environments/<env>/doppler-bootstrap/` stack (Terraform `doppler` provider) —
  **never** `kubectl create secret`, never hardcoded.
- **`kubectl_manifest.example_auth_dopplersecret`** — reference `DopplerSecret`
  (`auth-service-secrets` → `shared-core`) matching the Component 11 example.
  Rendered only when `create_example_dopplersecret = true`; real DopplerSecrets
  ship inside each service's chart/gitops manifests.

## Bootstrap chain (Component 11)

1. One-time per env: a platform operator runs the G3 `doppler-bootstrap` apply
   with a personal `DOPPLER_TOKEN`. That apply creates per-env, per-namespace
   service tokens **and** writes the long-lived Terraform service token back to
   Doppler at `ci/doppler_api_token`. The personal token is revoked immediately.
2. From the 2nd apply onward, CI reads `ci/doppler_api_token` from Doppler.
3. This module receives the per-namespace tokens via `bootstrap_service_tokens`
   and materialises them as the `doppler-token-<ns>` Secrets the operator reads.

## DopplerSecret reference shape (Component 11)

```yaml
apiVersion: secrets.doppler.com/v1alpha1
kind: DopplerSecret
metadata: { name: auth-service-secrets, namespace: shared-core }
spec:
  tokenSecret:   { name: doppler-token-shared-core, namespace: shared-core }
  project:       cypherx-platform
  config:        shared-core.auth
  managedSecret: { name: auth-service-secrets, namespace: shared-core, type: Opaque }
```

The resulting `auth-service-secrets` K8s Secret carries the auth-service env vars
(Component 20: `POSTGRES_DSN`, `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY`, `JWT_SIGNING_KID`).
`reloader` (Component 17b) rolls the Deployment when the managed Secret changes.

## Inputs (highlights)

| Variable                       | Default | Notes |
|--------------------------------|---------|-------|
| `environment`                  | —       | Selects Doppler config per env. |
| `chart_version`                | `1.6.0` | Operator chart pin. |
| `doppler_project`              | `cypherx-platform` | Component 20. |
| `bootstrap_service_tokens`     | `{}`    | **sensitive**, from G3 doppler provider. |
| `create_example_dopplersecret` | `false` | Render the reference CR. |

> **Never hardcode Doppler/AWS creds.** Tokens flow Doppler → G3 provider →
> `bootstrap_service_tokens` → K8s Secret. The Terraform-held Doppler API token
> itself lives in Doppler (`ci/doppler_api_token`, rotated every 90 days).
