###############################################################################
# Kong API Gateway — Component 8
#
# Install method: Helm (kong/kong chart, via Terraform).
# Mode: DB-less (declarative config via KongIngress/Kong CRDs — no separate DB).
# Version: Kong 3.6.x.
#
# Service type: LoadBalancer -> provisioned as an AWS ALB via the AWS Load
# Balancer Controller (k8s-addons/aws-lbc). TLS: the ALB terminates TLS with an
# ACM cert. Kong receives HTTP from the ALB on port 8000.
#
# mTLS boundary (intentional): ALB→Kong is plain HTTP inside the VPC private network. This is acceptable because (a) SG `sg-kong` only accepts traffic from `sg-alb`, (b) the path traverses only AWS-managed infra inside the VPC. Kong→backend services is mTLS via Istio (Kong runs with sidecar in `ingress` namespace). Do NOT remove this comment; future reviewers will otherwise "fix" it and break the deploy.
###############################################################################

resource "helm_release" "kong" {
  name             = "kong"
  repository       = "https://charts.konghq.com"
  chart            = "kong"
  version          = var.kong_chart_version
  namespace        = var.namespace
  create_namespace = false # `ingress` namespace created by Component 6.

  values = [yamlencode({
    # DB-less declarative mode (Component 8).
    env = {
      database = "off"
    }

    # Kong runs with the Istio sidecar in the `ingress` namespace, so the
    # Kong->backend hop is mTLS via Istio (see boundary comment above).
    deployment = {
      kong = {
        enabled = true
      }
    }

    replicaCount = var.replica_count

    # The ALB terminates TLS; Kong only needs the plaintext proxy listener.
    # Kong receives HTTP from the ALB on port 8000 (container) exposed as 80.
    proxy = {
      enabled = true
      type    = "LoadBalancer"

      http = {
        enabled       = true
        servicePort   = 80
        containerPort = 8000
      }

      # No TLS listener on Kong itself — the ALB does TLS termination (ACM).
      tls = {
        enabled = false
      }

      annotations = {
        # AWS Load Balancer Controller -> provision an ALB (Component 10).
        "kubernetes.io/ingress.class"                                  = "alb"
        "service.beta.kubernetes.io/aws-load-balancer-type"            = "external"
        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type" = "ip"
        "alb.ingress.kubernetes.io/scheme"                             = var.alb_scheme
        "alb.ingress.kubernetes.io/certificate-arn"                    = var.acm_certificate_arn
        "alb.ingress.kubernetes.io/listen-ports"                       = "[{\"HTTPS\":443},{\"HTTP\":80}]"
        "alb.ingress.kubernetes.io/ssl-redirect"                       = "443"
      }
    }

    # Admin API: cluster-internal only (health checklist: "Kong admin API
    # accessible from within cluster only"). Never exposed via the ALB.
    admin = {
      enabled = true
      type    = "ClusterIP"
      http = {
        enabled = true
      }
    }

    # Ingress controller enabled so routes can be declared as Kubernetes CRs
    # (KongPlugin / Ingress) as services come online in phases 2–9.
    ingressController = {
      enabled = true
    }
  })]
}

###############################################################################
# Base plugins (installed platform-wide) — Component 8.
#   - correlation-id        (inject X-Request-ID on every request)
#   - request-id            (unique ID per request)
#   - response-transformer  (inject standard response headers)
# Declared as cluster-wide KongClusterPlugin CRs (global: true) so they apply to
# every route without per-route wiring.
###############################################################################

# correlation-id: injects/propagates X-Request-ID (Contract 8 custom header).
resource "kubectl_manifest" "plugin_correlation_id" {
  yaml_body = yamlencode({
    apiVersion = "configuration.konghq.com/v1"
    kind       = "KongClusterPlugin"
    metadata = {
      name = "global-correlation-id"
      labels = {
        global = "true"
      }
      annotations = {
        "kubernetes.io/ingress.class" = "kong"
      }
    }
    plugin = "correlation-id"
    config = {
      header_name     = "X-Request-ID"
      generator       = "uuid"
      echo_downstream = true
    }
  })

  depends_on = [helm_release.kong]
}

# request-id: unique ID per request (Kong 3.x request-id plugin).
resource "kubectl_manifest" "plugin_request_id" {
  yaml_body = yamlencode({
    apiVersion = "configuration.konghq.com/v1"
    kind       = "KongClusterPlugin"
    metadata = {
      name = "global-request-id"
      labels = {
        global = "true"
      }
      annotations = {
        "kubernetes.io/ingress.class" = "kong"
      }
    }
    plugin = "request-id"
    config = {
      header_name = "X-Kong-Request-ID"
    }
  })

  depends_on = [helm_release.kong]
}

# response-transformer: inject standard response headers platform-wide.
resource "kubectl_manifest" "plugin_response_transformer" {
  yaml_body = yamlencode({
    apiVersion = "configuration.konghq.com/v1"
    kind       = "KongClusterPlugin"
    metadata = {
      name = "global-response-transformer"
      labels = {
        global = "true"
      }
      annotations = {
        "kubernetes.io/ingress.class" = "kong"
      }
    }
    plugin = "response-transformer"
    config = {
      add = {
        headers = [
          "X-CypherX-Gateway:kong",
        ]
      }
    }
  })

  depends_on = [helm_release.kong]
}

###############################################################################
# Route placeholders (routes added as services are deployed in phases 2–9).
# Encoded here as documentation; the actual Ingress/route CRs are created by
# each service's Helm chart when it deploys. The mapping is authoritative:
#
#   /v1/auth/*           -> shared-core/auth-service:8080
#   /v1/agents/*         -> shared-core/auth-service:8080   <- Auth owns agent identity (Phase 2: register, keys, token, /revoke-all-tokens)
#   /v1/tokens/*         -> shared-core/auth-service:8080   <- Auth owns token revocation
#   /v1/authorize        -> shared-core/auth-service:8080
#   /v1/service-tokens   -> shared-core/auth-service:8080   <- Contract 12 service token issuance
#   /v1/llms/*           -> shared-core/llms-gateway:8080
#   /v1/guardrails/*     -> shared-core/guardrails-service:8080
#   /v1/memory/*         -> shared-core/memory-service:8080
#   /v1/rag/*            -> shared-core/rag-service:8080
#   /v1/tasks/*          -> xagent/agent-runtime:8080       <- xAgent owns task execution
#   /v1/workflows/*      -> xagent/orchestrator:8080
#   /v1/platform/*       -> platform-mgmt/platform-service:8080
#
# Route ownership rule: `/v1/agents/*` is Auth, not xAgent. xAgent runs agent code but does NOT
# own the agent identity resource. Mixing these (e.g., routing `/v1/agents/*` to xagent) breaks the
# Contract 15 smoke test step 1 (`POST /v1/agents`) and every JWT mint call. Do not "fix" this routing
# by moving `/v1/agents/*` to xagent.
###############################################################################

locals {
  # Authoritative path -> backend map (consumed by service charts in phases 2–9).
  # Kept as data so a downstream stack can render Ingress CRs from a single source.
  route_map = {
    "/v1/auth"           = { namespace = "shared-core", service = "auth-service", port = 8080 }
    "/v1/agents"         = { namespace = "shared-core", service = "auth-service", port = 8080 } # Auth, NOT xAgent
    "/v1/tokens"         = { namespace = "shared-core", service = "auth-service", port = 8080 }
    "/v1/authorize"      = { namespace = "shared-core", service = "auth-service", port = 8080 }
    "/v1/service-tokens" = { namespace = "shared-core", service = "auth-service", port = 8080 }
    "/v1/llms"           = { namespace = "shared-core", service = "llms-gateway", port = 8080 }
    "/v1/guardrails"     = { namespace = "shared-core", service = "guardrails-service", port = 8080 }
    "/v1/memory"         = { namespace = "shared-core", service = "memory-service", port = 8080 }
    "/v1/rag"            = { namespace = "shared-core", service = "rag-service", port = 8080 }
    "/v1/tasks"          = { namespace = "xagent", service = "agent-runtime", port = 8080 } # xAgent owns task execution
    "/v1/workflows"      = { namespace = "xagent", service = "orchestrator", port = 8080 }
    "/v1/platform"       = { namespace = "platform-mgmt", service = "platform-service", port = 8080 }
  }
}
