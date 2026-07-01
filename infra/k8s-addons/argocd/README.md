# k8s-addons/argocd — Component 12 (GitOps)

Installs ArgoCD via Helm, registers the `cypherx-gitops` repository, and creates
the App-of-Apps root that drives all per-service deployments.

## What it deploys

- **`helm_release.argocd`** — `argo/argo-cd` chart `7.3.x` (ships ArgoCD app
  **2.11.x**, per Component 12). Namespace `argocd` (istio-injection disabled —
  ArgoCD is bootstrapped *before* Istio). Server runs `insecure` behind the
  internal ALB (TLS terminated at the ALB; VPN-only per Component 5).
- **`kubernetes_secret.gitops_repo`** — repository credential labelled
  `argocd.argoproj.io/secret-type=repository`. The HTTPS deploy token/password
  comes from `var.gitops_repo_password` (sourced from **Doppler**, never
  hardcoded — Component 18 `cypherx-gitops-bot` installation token).
- **`kubectl_manifest.app_of_apps`** — root `Application` `cypherx-platform`
  pointing at the gitops repo's `apps/dev-apps.yaml` (Component 12/19), which
  watches `gitops/envs/<env>/` and creates child apps automatically.

## Sync policy (Component 12)

| Environment    | Policy |
|----------------|--------|
| `dev`/`staging`| **Automated** — `automated.prune=true`, `automated.selfHeal=true` |
| `prod`         | **Manual** — no `automated` block; sync/approval done in the ArgoCD UI |

Driven by `local.automated_sync = contains(["dev","staging"], var.environment)`.

## Inputs (highlights)

| Variable                 | Default | Notes |
|--------------------------|---------|-------|
| `environment`            | —       | Drives sync policy + hostname. |
| `chart_version`          | `7.3.11`| argo-cd chart pinned to ArgoCD 2.11.x. |
| `gitops_repo_url`        | cypherx-gitops | Registered repo. |
| `gitops_repo_password`   | —       | **sensitive**, from Doppler. Required input. |
| `app_of_apps_path`       | `apps/dev-apps.yaml` | App-of-Apps manifest path. |
| `server_host`            | derived | `argocd.<env>.cypherx.ai`. |
| `create_namespace`       | `false` | Namespace owned by the namespaces module. |

## Secret sourcing

`gitops_repo_password` MUST be supplied by the calling Terragrunt stack from
Doppler (the `cypherx-gitops-bot` GitHub App installation token, rotated per
Component 18). No credential is ever committed.

## Provider note

Uses `gavinbunney/kubectl` for the `Application` CRD (the CRD is installed by the
argo-cd chart in the same apply; `kubectl_manifest` tolerates the
apply-time-unknown CRD better than `kubernetes_manifest`, which requires the CRD
to exist at plan time).
