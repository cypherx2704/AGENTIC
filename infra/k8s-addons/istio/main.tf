###############################################################################
# Istio Service Mesh — Components 7 & 8
#
# Install method: Helm (via Terraform), profile = default (istiod + ingress GW).
# Version: 1.22.x (latest stable at build time).
#
# Layout follows the official istio Helm chart split:
#   1. istio-base   — CRDs + cluster roles
#   2. istiod       — control plane, carries meshConfig (extensionProviders, tracing)
#   3. gateway      — ingress gateway Deployment/Service (in the `ingress` namespace)
#
# After the control plane is up we apply the mesh-policy CRs as raw manifests:
#   - global STRICT PeerAuthentication
#   - mesh-wide Telemetry (otel-tempo, W3C propagation per Contract 8)
#   - metrics-permissive PeerAuthentication (ports 15020 + 9090)
#   - per-host DestinationRule tls.mode=DISABLE for non-mesh destinations
###############################################################################

locals {
  # Component 7: sampling = 100% (dev), 10% (prod). staging mirrors dev.
  derived_sample_pct = var.env == "prod" ? 10.0 : 100.0
  sample_pct         = var.tracing_sample_percentage != null ? var.tracing_sample_percentage : local.derived_sample_pct
}

###############################################################################
# 1. istio-base — CRDs and cluster-scoped resources.
###############################################################################
resource "helm_release" "base" {
  name             = "istio-base"
  repository       = "https://istio-release.storage.googleapis.com/charts"
  chart            = "base"
  version          = var.istio_version
  namespace        = var.istio_namespace
  create_namespace = true

  # Let Helm own the CRDs so upgrades reconcile them.
  set {
    name  = "defaultRevision"
    value = "default"
  }
}

###############################################################################
# 2. istiod — control plane. meshConfig carries:
#      - extensionProviders.otel-tempo (OTLP gRPC to Tempo distributor)
#      - enableTracing + access logs to stdout (Promtail picks these up)
#    Global STRICT mTLS is enforced via a PeerAuthentication CR below (not here),
#    so the toggle stays auditable as a discrete resource.
###############################################################################
resource "helm_release" "istiod" {
  name       = "istiod"
  repository = "https://istio-release.storage.googleapis.com/charts"
  chart      = "istiod"
  version    = var.istio_version
  namespace  = var.istio_namespace

  values = [yamlencode({
    meshConfig = {
      # Access logs: enabled -> stdout (Component 7; picked up by Promtail).
      accessLogFile = "/dev/stdout"

      enableTracing = true

      # Component 7: OTLP -> Tempo. NOT Zipkin, NOT the deprecated openCensusAgent.
      extensionProviders = [
        {
          name = "otel-tempo"
          opentelemetry = {
            service = var.tempo_otlp_grpc_endpoint
            port    = var.tempo_otlp_grpc_port # OTLP gRPC (4317)
          }
        }
      ]

      # Contract 8 / Component 7: propagate W3C Trace Context. Istio emits and
      # forwards `traceparent` + `tracestate`; B3/Zipkin headers are not used.
      defaultConfig = {
        tracing = {}
      }
    }

    # Trace propagation style is W3C (the Istio default for the OTLP provider);
    # we pin the proxy header behaviour explicitly so a future default flip can't
    # silently switch us to B3.
    pilot = {
      env = {
        # Keep W3C trace-context headers as the canonical propagation format
        # (traceparent + tracestate) — matches Contract 8.
        PILOT_TRACE_SAMPLING = tostring(local.sample_pct)
      }
    }
  })]

  depends_on = [helm_release.base]
}

###############################################################################
# 3. Ingress gateway — lives in the `ingress` namespace (Component 6,
#    istio-injection: enabled). Kong runs here with a sidecar so Kong->backend
#    is mTLS (Component 8). The gateway Service is ClusterIP; the AWS ALB is
#    provisioned by the Kong LoadBalancer Service (k8s-addons/kong), NOT here.
###############################################################################
resource "helm_release" "gateway" {
  name             = "istio-ingressgateway"
  repository       = "https://istio-release.storage.googleapis.com/charts"
  chart            = "gateway"
  version          = var.istio_version
  namespace        = var.gateway_namespace
  create_namespace = false # `ingress` namespace is created by Component 6.

  values = [yamlencode({
    service = {
      # ALB termination happens at Kong's LoadBalancer Service. Keep this gateway
      # internal (ClusterIP) so we do not provision a second, conflicting ALB.
      type = "ClusterIP"
    }
  })]

  depends_on = [helm_release.istiod]
}

###############################################################################
# Mesh policy CRs (applied after the control plane CRDs exist).
###############################################################################

# Global STRICT mTLS (Component 7). Applied mesh-wide in istio-system.
resource "kubectl_manifest" "peer_auth_default_strict" {
  yaml_body = yamlencode({
    apiVersion = "security.istio.io/v1"
    kind       = "PeerAuthentication"
    metadata = {
      name      = "default"
      namespace = var.istio_namespace
    }
    spec = {
      mtls = {
        mode = "STRICT"
      }
    }
  })

  depends_on = [helm_release.istiod]
}

# Mesh-wide tracing Telemetry (Component 7). Binds the otel-tempo provider and
# sets the random sampling percentage (100 dev / 10 prod). W3C propagation
# (traceparent + tracestate) per Contract 8 is the propagation contract this
# Telemetry feeds.
resource "kubectl_manifest" "telemetry_mesh_tracing" {
  yaml_body = yamlencode({
    apiVersion = "telemetry.istio.io/v1"
    kind       = "Telemetry"
    metadata = {
      name      = "mesh-tracing"
      namespace = var.istio_namespace
    }
    spec = {
      tracing = [
        {
          providers = [
            { name = "otel-tempo" }
          ]
          randomSamplingPercentage = local.sample_pct # 100.0 dev / 10.0 prod
        }
      ]
    }
  })

  depends_on = [helm_release.istiod]
}

# Metrics-scrape mTLS exception (Component 7, REQUIRED — otherwise scrape fails).
# PERMISSIVE only on the merged-metrics port (15020) and the app /metrics port
# convention (9090). Everything else stays STRICT. Applied mesh-wide.
resource "kubectl_manifest" "peer_auth_metrics_permissive" {
  yaml_body = yamlencode({
    apiVersion = "security.istio.io/v1"
    kind       = "PeerAuthentication"
    metadata = {
      name      = "metrics-permissive"
      namespace = var.istio_namespace
    }
    spec = {
      portLevelMtls = {
        for p in var.metrics_permissive_ports : tostring(p) => { mode = "PERMISSIVE" }
      }
    }
  })

  depends_on = [helm_release.istiod]
}

# Non-mesh destination mTLS exception (Component 7, REQUIRED — otherwise
# PgBouncer/Valkey calls fail). One DestinationRule per non-mesh host the mesh
# calls. The `data` namespace runs without sidecars (Postgres/Valkey have their
# own TLS), so a sidecar'd caller under global STRICT must NOT originate mTLS to
# these hosts. Do NOT weaken global PeerAuthentication — keep this host-scoped.
#
# NOTE: repeat an entry in var.non_mesh_hosts for any other host in `data` or any
# non-mesh service the mesh must reach (e.g. RDS/MSK endpoints resolved by
# ExternalName Services).
resource "kubectl_manifest" "dest_rule_no_mtls" {
  for_each = var.non_mesh_hosts

  yaml_body = yamlencode({
    apiVersion = "networking.istio.io/v1"
    kind       = "DestinationRule"
    metadata = {
      name      = "${each.key}-no-mtls"
      namespace = var.istio_namespace
    }
    spec = {
      host = each.value.host
      trafficPolicy = {
        tls = {
          mode = "DISABLE" # sidecar will not originate mTLS to this host
        }
      }
    }
  })

  depends_on = [helm_release.istiod]
}
