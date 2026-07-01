# ─────────────────────────────────────────────────────────────────────────────
# Components 6 & 7 — Default-deny NetworkPolicies + explicit-allow placeholders
#
# Component 6: "Deny all ingress from other namespaces by default. Explicit allow
#              rules per namespace (defined in k8s-addons/network-policies/)."
#
# Component 7 baseline (mirrored at L3/L4 here; Istio AuthorizationPolicies do the
# L7 equivalent):
#   - observability namespace can scrape /metrics from all pods
#   - argocd can deploy to all namespaces
#
# These are NetworkPolicies (CNI-enforced). The Istio AuthorizationPolicies in the
# istio addon (G4) are the mesh-layer counterpart. Both layers are defence-in-depth.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Namespaces that get a default deny-all-ingress baseline.
  guarded_namespaces = toset(var.namespaces)
}

# ── 1. Default deny-all-ingress, one per namespace ───────────────────────────
# An empty podSelector selects ALL pods; an empty (omitted) ingress block with
# policyTypes=["Ingress"] denies all inbound traffic. Egress is left untouched so
# pods can still reach DNS, the API server, RDS/MSK/Valkey, and external APIs.
resource "kubernetes_network_policy" "default_deny_ingress" {
  for_each = local.guarded_namespaces

  metadata {
    name      = "default-deny-ingress"
    namespace = each.value
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "cypherx.ai/component"         = "network-policies"
      "cypherx.ai/environment"       = var.environment
      "cypherx.ai/policy-tier"       = "baseline-deny"
    }
  }

  spec {
    # Empty podSelector = applies to every pod in the namespace.
    pod_selector {}

    policy_types = ["Ingress"]
    # No ingress rules => deny all ingress. Explicit allows below add back the
    # minimum required paths.
  }
}

# ── 2. Allow intra-namespace traffic (so sidecars/services in the same ns talk) ─
# Without this, default-deny would also block same-namespace pod-to-pod, breaking
# Istio sidecar <-> app and multi-pod services. Scoped to the same namespace only.
resource "kubernetes_network_policy" "allow_same_namespace" {
  for_each = local.guarded_namespaces

  metadata {
    name      = "allow-same-namespace"
    namespace = each.value
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "cypherx.ai/component"         = "network-policies"
      "cypherx.ai/environment"       = var.environment
      "cypherx.ai/policy-tier"       = "baseline-allow"
    }
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress"]

    ingress {
      from {
        # Pods in the SAME namespace (the namespaceSelector matches this ns by name).
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = each.value
          }
        }
      }
    }
  }
}

# ── 3. PLACEHOLDER: allow observability namespace to scrape /metrics everywhere ─
# Component 7: "observability namespace can scrape /metrics from all pods (GET only)".
# L3/L4 here opens the metrics port FROM the observability namespace TO every guarded
# namespace. The L7 GET-only restriction is enforced by the Istio AuthorizationPolicy.
# Gated behind enable_explicit_allows so first-cycle ships deny-all only.
resource "kubernetes_network_policy" "allow_observability_scrape" {
  for_each = var.enable_explicit_allows ? local.guarded_namespaces : toset([])

  metadata {
    name      = "allow-observability-scrape"
    namespace = each.value
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "cypherx.ai/component"         = "network-policies"
      "cypherx.ai/environment"       = var.environment
      "cypherx.ai/policy-tier"       = "explicit-allow"
      "cypherx.ai/allow"             = "observability-scrape"
    }
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress"]

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = var.observability_namespace
          }
        }
      }

      # App metrics port convention (Component 13 / Contract 7).
      ports {
        port     = var.metrics_port
        protocol = "TCP"
      }
      # Istio merged-metrics port (Component 7 PeerAuthentication PERMISSIVE list).
      ports {
        port     = 15020
        protocol = "TCP"
      }
    }
  }
}

# ── 4. PLACEHOLDER: allow argocd to deploy/sync into workload namespaces ────────
# Component 7: "argocd can deploy to all namespaces". ArgoCD's application-controller
# and repo-server live in the argocd namespace; this opens ingress FROM argocd into
# each guarded namespace. (ArgoCD primarily talks to the K8s API server, but app
# health-checks / hooks may hit workload pods directly.)
# Gated behind enable_explicit_allows.
resource "kubernetes_network_policy" "allow_argocd_deploy" {
  for_each = var.enable_explicit_allows ? local.guarded_namespaces : toset([])

  metadata {
    name      = "allow-argocd-deploy"
    namespace = each.value
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "cypherx.ai/component"         = "network-policies"
      "cypherx.ai/environment"       = var.environment
      "cypherx.ai/policy-tier"       = "explicit-allow"
      "cypherx.ai/allow"             = "argocd-deploy"
    }
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress"]

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = var.argocd_namespace
          }
        }
      }
    }
  }
}
