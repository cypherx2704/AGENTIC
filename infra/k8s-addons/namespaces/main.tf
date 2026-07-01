# ─────────────────────────────────────────────────────────────────────────────
# Component 6 — Kubernetes Namespaces
#
# Encodes the EXACT namespace set + istio-injection labels from
# phase-01-infrastructure.md Component 6 (lines 359-378). The istio-injection
# value per namespace is load-bearing:
#
#   ingress         istio-injection: enabled
#   istio-system    (managed by Istio — created by the istio addon, NOT here)
#   shared-core     istio-injection: enabled
#   xagent          istio-injection: enabled
#   tools           istio-injection: enabled
#   platform-mgmt   istio-injection: enabled
#   data            istio-injection: disabled  (PgBouncer/Valkey endpoint refs)
#   messaging       (no pods — just ConfigMaps with broker addresses)
#   observability   istio-injection: disabled  (avoids circular dependency)
#   argocd          istio-injection: disabled  (bootstrapped before Istio)
#   px0-bridge      istio-injection: enabled
#
# NOTE: istio-system is owned by the Istio addon (G4) and is intentionally NOT
# created here to avoid a two-owner conflict. The Istio Helm release creates it.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Map of namespace -> istio sidecar-injection mode.
  #   "enabled"  → label istio-injection=enabled
  #   "disabled" → label istio-injection=disabled (explicit opt-out)
  #   "none"     → no istio-injection label at all (messaging has no pods)
  namespaces = {
    ingress = {
      injection = "enabled"
      purpose   = "Istio ingress gateway + Kong (sidecar-injected edge)"
    }
    shared-core = {
      injection = "enabled"
      purpose   = "auth, llms-gateway, guardrails, memory, rag services"
    }
    xagent = {
      injection = "enabled"
      purpose   = "agent-runtime + orchestrator (task execution)"
    }
    tools = {
      injection = "enabled"
      purpose   = "tool-* MCP servers (Phase 7+)"
    }
    platform-mgmt = {
      injection = "enabled"
      purpose   = "platform-management service"
    }
    data = {
      # disabled: Postgres/Valkey carry their own TLS, not mesh mTLS. Component 7
      # adds a DestinationRule tls.mode=DISABLE so the mesh can reach pgbouncer here.
      injection = "disabled"
      purpose   = "PgBouncer + Valkey/RDS/MSK endpoint references (no mesh sidecar)"
    }
    messaging = {
      # none: no pods run here in Phase 1 — only ConfigMaps with broker addresses.
      injection = "none"
      purpose   = "Kafka broker-address ConfigMaps (Schema Registry lands here later)"
    }
    observability = {
      # disabled: Prometheus/Loki/Tempo scrape the mesh; injecting them creates a
      # circular dependency (sidecar needs istiod, istiod metrics need scraping).
      injection = "disabled"
      purpose   = "kube-prometheus-stack, Loki, Tempo, Grafana (no mesh sidecar)"
    }
    argocd = {
      # disabled: ArgoCD is bootstrapped BEFORE Istio, so it cannot depend on a sidecar.
      injection = "disabled"
      purpose   = "ArgoCD GitOps control plane (bootstrapped before mesh)"
    }
    px0-bridge = {
      injection = "enabled"
      purpose   = "px0 org/billing lifecycle bridge (Contract 13 adapter)"
    }
  }
}

resource "kubernetes_namespace" "this" {
  for_each = local.namespaces

  metadata {
    name = each.key

    labels = merge(
      var.common_labels,
      {
        "app.kubernetes.io/managed-by" = "terraform"
        "cypherx.ai/component"         = "namespaces"
        "cypherx.ai/environment"       = var.environment
        "kubernetes.io/metadata.name"  = each.key
      },
      # Only stamp the istio-injection label when injection is enabled/disabled.
      # "none" (messaging) gets no label at all.
      each.value.injection == "none" ? {} : {
        "istio-injection" = each.value.injection
      },
    )

    annotations = {
      "cypherx.ai/purpose" = each.value.purpose
    }
  }
}
