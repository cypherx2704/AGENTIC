###############################################################################
# external-dns — Component 17b
#
# Install method: Helm (external-dns/external-dns).
# Purpose: watches K8s Ingress/Service annotations -> creates Route53 records
# (Component 5: api.<env>.cypherx.ai, auth.<env>.cypherx.ai, etc.).
# Without it every new ingress hostname needs a manual Route53 entry.
#
# IRSA: ServiceAccount bound to the ExternalDNS role from modules/iam
# (var.irsa_role_arn) — Route53 change/list permissions on the cypherx.ai zone.
###############################################################################

locals {
  txt_owner_id = var.txt_owner_id != null ? var.txt_owner_id : "cypherx-${var.env}"
}

resource "helm_release" "external_dns" {
  name       = "external-dns"
  repository = "https://kubernetes-sigs.github.io/external-dns/"
  chart      = "external-dns"
  version    = var.chart_version
  namespace  = var.namespace

  values = [yamlencode({
    provider = {
      name = "aws"
    }

    # Route53 provider scoped to the cypherx.ai hosted zone.
    domainFilters = [var.domain_filter]

    aws = {
      region = "us-east-1"
      # zoneType left empty -> manage both public/private as discovered.
      zoneTagFilter = []
    }
    zoneIdFilters = [var.route53_zone_id]

    # Watch Ingress AND Service annotations (Component 17b).
    sources = ["ingress", "service"]

    # TXT-registry ownership so envs/clusters never clobber each other's records.
    policy     = "sync"
    registry   = "txt"
    txtOwnerId = local.txt_owner_id

    # IRSA: annotate the SA with the role from modules/iam.
    serviceAccount = {
      create = true
      name   = var.service_account_name
      annotations = {
        "eks.amazonaws.com/role-arn" = var.irsa_role_arn
      }
    }

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
