###############################################################################
# metrics-server — Component 17b
#
# Install method: Helm (metrics-server/metrics-server).
# Purpose: required by HPA to read pod CPU/memory metrics. Without it, all HPAs
# sit at "unknown" and never scale.
###############################################################################

resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  version    = var.chart_version
  namespace  = var.namespace

  values = [yamlencode({
    replicas = var.replicas

    args = [
      # Standard kubelet-preferred address ordering for EKS.
      "--kubelet-preferred-address-types=InternalIP",
    ]

    # Run on system nodes (Component 4).
    nodeSelector = {
      "node-role" = "system"
    }
    tolerations = [
      {
        key      = "CriticalAddonsOnly"
        operator = "Equal"
        value    = "true"
        effect   = "NoSchedule"
      }
    ]
  })]
}
