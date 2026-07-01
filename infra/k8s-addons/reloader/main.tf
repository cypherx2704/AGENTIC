###############################################################################
# reloader (stakater/reloader) — Component 17b
#
# Install method: Helm (stakater/reloader).
# Purpose: watches ConfigMap/Secret changes -> rolls Deployments that reference
# them. Without it, rotated Doppler secrets (Component 11) do not propagate to
# running pods without a manual restart.
###############################################################################

resource "helm_release" "reloader" {
  name       = "reloader"
  repository = "https://stakater.github.io/stakater-charts"
  chart      = "reloader"
  version    = var.chart_version
  namespace  = var.namespace

  values = [yamlencode({
    reloader = {
      # Opt-in mode: only roll workloads that carry the reloader annotation
      # (reloader.stakater.com/auto or .../search). Avoids surprise restarts of
      # workloads that did not ask for it.
      watchGlobally = true

      deployment = {
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
      }
    }
  })]
}
