# ─────────────────────────────────────────────────────────────────────────────
# Component 11 — Doppler Operator
#
#   Install method: Helm (doppler/doppler-kubernetes-operator)
#   Purpose:        sync Doppler secrets -> K8s Secret objects
#
#   Doppler service tokens: one per (env, namespace), scoped to that namespace's
#   config. Stored as K8s Secrets (the operator bootstrap secret) — PROVISIONED BY
#   TERRAFORM using the Doppler Terraform provider (the G3 doppler-bootstrap stack),
#   NOT manual kubectl. The Doppler API token used by Terraform lives in Doppler
#   itself (ci/doppler_api_token, CI-only, rotated every 90 days).
#
#   Initial bootstrap (one-time, per env): see the G3 environments/<env>/
#   doppler-bootstrap/ stack — a platform operator runs the first apply with a
#   personal DOPPLER_TOKEN, which (a) creates the per-env service tokens and
#   (b) writes the long-lived Terraform service token back to Doppler at
#   ci/doppler_api_token. The personal token is revoked immediately after.
# ─────────────────────────────────────────────────────────────────────────────

# ── Doppler operator (Helm) ───────────────────────────────────────────────────
resource "helm_release" "doppler_operator" {
  name             = "doppler-operator"
  namespace        = var.namespace
  create_namespace = var.create_namespace

  repository = "https://helm.doppler.com"
  chart      = "doppler-kubernetes-operator"
  version    = var.chart_version

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 600

  values = [
    yamlencode({
      # Run the controller on core nodes (control-plane-ish workload).
      nodeSelector = {
        "node-role" = "core"
      }
    }),
  ]
}

# ── Per-namespace bootstrap service-token Secrets ─────────────────────────────
# One K8s Secret per (env, namespace), each holding the namespace-scoped Doppler
# service token produced by the G3 doppler-bootstrap stack. The DopplerSecret CRs
# (one per service, shipped with the service) reference these by name.
#
# The token VALUES come from var.bootstrap_service_tokens (Doppler provider output
# in G3). Nothing here is hardcoded.
resource "kubernetes_secret" "namespace_bootstrap" {
  for_each = var.bootstrap_service_tokens

  metadata {
    name      = "doppler-token-${each.key}"
    namespace = each.key
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "cypherx.ai/component"         = "doppler-operator"
      "cypherx.ai/environment"       = var.environment
    }
    annotations = {
      # Component 11: provisioned by Terraform (doppler provider), not kubectl.
      "cypherx.ai/source" = "terraform-doppler-provider"
    }
  }

  type = "Opaque"

  data = {
    serviceToken = each.value
  }

  depends_on = [helm_release.doppler_operator]
}

# ── Reference DopplerSecret CR (Component 11 example: auth-service-secrets) ────
# Component 11 example:
#   managedSecret.name:      auth-service-secrets
#   managedSecret.namespace: shared-core
#   -> K8s Secret created with all auth-service env vars
#
# This is the REFERENCE shape only (create_example_dopplersecret defaults false).
# Real DopplerSecrets ship inside each service's Helm chart / gitops manifests.
resource "kubectl_manifest" "example_auth_dopplersecret" {
  count = var.create_example_dopplersecret ? 1 : 0

  yaml_body = yamlencode({
    apiVersion = "secrets.doppler.com/v1alpha1"
    kind       = "DopplerSecret"
    metadata = {
      name      = "auth-service-secrets"
      namespace = "shared-core"
      labels = {
        "app.kubernetes.io/managed-by" = "terraform"
        "cypherx.ai/component"         = "doppler-operator"
        "cypherx.ai/example"           = "true"
      }
    }
    spec = {
      # Reference the namespace-scoped bootstrap token Secret created above.
      tokenSecret = {
        name      = "doppler-token-shared-core"
        namespace = "shared-core"
      }
      # Doppler source: project cypherx-platform, config shared-core.auth (per env).
      project = var.doppler_project
      config  = "shared-core.auth"
      # Resulting K8s Secret the auth-service Deployment mounts.
      managedSecret = {
        name      = "auth-service-secrets"
        namespace = "shared-core"
        # Recreate on Doppler change so reloader (Component 17b) can roll the pod.
        type = "Opaque"
      }
    }
  })

  depends_on = [
    helm_release.doppler_operator,
    kubernetes_secret.namespace_bootstrap,
  ]
}
