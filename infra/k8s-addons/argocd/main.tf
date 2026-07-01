# ─────────────────────────────────────────────────────────────────────────────
# Component 12 — ArgoCD (GitOps)
#
#   Install method: Helm (argo/argo-cd)
#   Namespace:      argocd
#   Version:        2.11.x   (chart 7.3.x ships ArgoCD app 2.11.x)
#
#   Repositories registered: cypherx-gitops (GitHub, HTTPS with deploy key)
#
#   App-of-Apps: Root App "cypherx-platform" watches gitops/envs/<env>/ and
#                creates child apps automatically (apps/dev-apps.yaml).
#
#   Sync policy (dev/staging): Automated (self-heal + prune)
#   Sync policy (prod):        Manual approval required in ArgoCD UI
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Component 12: dev/staging automated (self-heal + prune); prod manual.
  automated_sync = contains(["dev", "staging"], var.environment)

  server_host = var.server_host != "" ? var.server_host : "argocd.${var.environment}.cypherx.ai"

  # Repository credential Secret — labelled so ArgoCD's repo-server discovers it.
  # The password is sourced from a variable (Doppler), never committed.
  repo_secret_name = "cypherx-gitops-repo"
}

# ── ArgoCD core install ───────────────────────────────────────────────────────
resource "helm_release" "argocd" {
  name             = "argo-cd"
  namespace        = var.namespace
  create_namespace = var.create_namespace

  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = var.chart_version

  # ArgoCD is bootstrapped before Istio; it must come up cleanly on its own.
  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  values = [
    yamlencode({
      global = {
        # No istio sidecar in argocd ns (Component 6: istio-injection disabled).
        podLabels = {
          "cypherx.ai/component" = "argocd"
        }
      }

      configs = {
        cm = {
          "application.instanceLabelKey" = "argocd.argoproj.io/instance"
          url                            = "https://${local.server_host}"
          # Allow the App-of-Apps to live in the same cluster it manages.
          "resource.exclusions" = yamlencode([
            {
              apiGroups = ["cilium.io"]
              kinds     = ["CiliumIdentity"]
              clusters  = ["*"]
            }
          ])
        }
        params = {
          # Internal ALB / VPN-only (Component 5). TLS terminated at the ALB; the
          # ArgoCD server runs insecure behind it (server.insecure=true).
          "server.insecure" = true
        }
        # Repository registration via the declarative repositories key. The actual
        # secret is created below and referenced by name; credentials come from
        # Doppler-sourced variables.
        repositories = {
          (local.repo_secret_name) = {
            url  = var.gitops_repo_url
            name = "cypherx-gitops"
            type = "git"
          }
        }
      }

      server = {
        replicas = var.environment == "prod" ? 2 : 1
        # Pin ArgoCD control plane onto core nodes (NOT agent/spot).
        nodeSelector = {
          "node-role" = "core"
        }
      }

      controller = {
        replicas = 1
        nodeSelector = {
          "node-role" = "core"
        }
      }

      repoServer = {
        replicas = var.environment == "prod" ? 2 : 1
        nodeSelector = {
          "node-role" = "core"
        }
      }

      # Dex/SSO and notifications configured later; keep the bootstrap minimal.
      dex = {
        enabled = false
      }
    }),
    var.extra_values,
  ]
}

# ── Repository credential Secret (HTTPS deploy key/token from Doppler) ─────────
# Labelled argocd.argoproj.io/secret-type=repository so ArgoCD picks it up.
resource "kubernetes_secret" "gitops_repo" {
  metadata {
    name      = local.repo_secret_name
    namespace = var.namespace
    labels = {
      "argocd.argoproj.io/secret-type" = "repository"
      "app.kubernetes.io/managed-by"   = "terraform"
      "cypherx.ai/component"           = "argocd"
    }
  }

  data = {
    type     = "git"
    url      = var.gitops_repo_url
    name     = "cypherx-gitops"
    username = var.gitops_repo_username
    password = var.gitops_repo_password
  }

  type = "Opaque"

  depends_on = [helm_release.argocd]
}

# ── App-of-Apps root Application ──────────────────────────────────────────────
# Root "cypherx-platform" points at the gitops repo's apps/dev-apps.yaml, which
# in turn watches gitops/envs/<env>/ and creates the per-service child apps.
# Sync policy: automated (self-heal + prune) for dev/staging; manual for prod.
resource "kubectl_manifest" "app_of_apps" {
  yaml_body = yamlencode({
    apiVersion = "argoproj.io/v1alpha1"
    kind       = "Application"
    metadata = {
      name      = "cypherx-platform"
      namespace = var.namespace
      labels = {
        "cypherx.ai/component"   = "argocd"
        "cypherx.ai/environment" = var.environment
      }
      finalizers = ["resources-finalizer.argocd.argoproj.io"]
    }
    spec = {
      project = "default"
      source = {
        repoURL        = var.gitops_repo_url
        targetRevision = var.gitops_target_revision
        path           = "."
        directory = {
          recurse = false
          include = var.app_of_apps_path
        }
      }
      destination = {
        server    = "https://kubernetes.default.svc"
        namespace = var.namespace
      }
      syncPolicy = local.automated_sync ? {
        # dev/staging: automated self-heal + prune.
        automated = {
          prune    = true
          selfHeal = true
        }
        syncOptions = ["CreateNamespace=false", "ApplyOutOfSyncOnly=true"]
        } : {
        # prod: NO automated block => manual sync/approval in the ArgoCD UI.
        syncOptions = ["CreateNamespace=false"]
      }
    }
  })

  depends_on = [
    helm_release.argocd,
    kubernetes_secret.gitops_repo,
  ]
}
