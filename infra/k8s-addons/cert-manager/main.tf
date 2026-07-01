###############################################################################
# cert-manager — Component 9
#
# Purpose: manage TLS certificates for INTERNAL developer-facing dashboard
# ingresses ONLY.
#
# Scope boundaries (do NOT widen):
#   - ALB certs      -> managed by AWS ACM (auto-renew). NOT cert-manager.
#   - Istio certs    -> managed by the Istio CA. NOT cert-manager.
#   - cert-manager   -> developer-facing dashboard ingresses only
#                       (e.g. grafana.<env>.cypherx.ai, argocd.<env>.cypherx.ai
#                       behind the internal/VPN-only ingress).
###############################################################################

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.chart_version
  namespace        = var.namespace
  create_namespace = true

  values = [yamlencode({
    # Install the CRDs with the chart so the ClusterIssuer below can apply.
    crds = {
      enabled = true
    }
  })]
}

# ClusterIssuer: letsencrypt-prod — for any INTERNAL dashboard TLS only.
# HTTP-01 solver routes through the internal ingress class (Kong), never the
# public ACM-terminated ALB.
resource "kubectl_manifest" "clusterissuer_letsencrypt_prod" {
  yaml_body = yamlencode({
    apiVersion = "cert-manager.io/v1"
    kind       = "ClusterIssuer"
    metadata = {
      name = "letsencrypt-prod"
    }
    spec = {
      acme = {
        server = var.letsencrypt_acme_server
        email  = var.acme_email
        privateKeySecretRef = {
          name = "letsencrypt-prod-account-key"
        }
        solvers = [
          {
            http01 = {
              ingress = {
                class = var.solver_ingress_class
              }
            }
          }
        ]
      }
    }
  })

  depends_on = [helm_release.cert_manager]
}
