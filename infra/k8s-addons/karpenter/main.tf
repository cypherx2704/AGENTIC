# ─────────────────────────────────────────────────────────────────────────────
# Components 4 & 17b — Karpenter
#
#   Install: helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter
#            --version v1.x
#   CRDs:    NodePool + EC2NodeClass (NOT the deprecated Provisioner CRD).
#
#   NodePools (Component 4 — single source of truth; do NOT also create managed
#   node groups for these roles or the two scalers fight):
#     ┌──────────┬──────────────────────────┬──────────────────────┐
#     │ NodePool │ Instance shape           │ Capacity             │
#     ├──────────┼──────────────────────────┼──────────────────────┤
#     │ core     │ c5.xlarge family         │ on-demand            │
#     │ agent ⚡ │ c5.2xlarge / c6i family  │ on-demand + spot     │
#     │ tools    │ c5.large / c6i family    │ on-demand + spot     │
#     └──────────┴──────────────────────────┴──────────────────────┘
#
#   Node labels (Component 4): node-role=core | agent | tools
#
#   NON-OVERLAP GUARD: there is intentionally NO `observability` and NO `system`
#   NodePool here. Those roles are EKS-managed node groups (Component 4) — pinned
#   PVCs (observability) and Karpenter's own host (system). Creating a Karpenter
#   NodePool for either would overlap a managed NG and the two scalers would fight
#   (managed-NG ASG adds a node, Karpenter consolidates it minutes later, repeat).
#   "do NOT consolidate observability" is honoured by simply never managing it here.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  discovery_value = var.discovery_tag_value != "" ? var.discovery_tag_value : var.cluster_name

  # Karpenter EC2NodeClass requires EXACTLY ONE of `role` or `instanceProfile`.
  # Emitting the other as null still serialises a key (yamlencode keeps nulls) and
  # the CRD rejects it, so build the identity fragment conditionally and merge it.
  node_identity = var.instance_profile_name == "" ? {
    role = var.node_iam_role_name
    } : {
    instanceProfile = var.instance_profile_name
  }
}

# ── Karpenter controller (OCI Helm chart) ─────────────────────────────────────
# Runs in kube-system on the system managed NG (Karpenter cannot provision its own
# host node — Component 4). The controller IRSA role comes from the G3 IAM stack.
resource "helm_release" "karpenter" {
  name      = "karpenter"
  namespace = var.namespace

  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = var.chart_version

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  values = [
    yamlencode({
      settings = {
        clusterName     = var.cluster_name
        clusterEndpoint = var.cluster_endpoint
        # Interruption queue (spot rebalance/termination) — named by the G3 stack
        # as the cluster name by convention.
        interruptionQueue = var.cluster_name
      }

      serviceAccount = {
        annotations = {
          "eks.amazonaws.com/role-arn" = var.controller_role_arn
        }
      }

      # Pin the controller to the system managed NG (it cannot run on the nodes it
      # provisions). system-nodes carry the CriticalAddonsOnly taint.
      nodeSelector = {
        "node-role" = "system"
      }
      tolerations = [
        {
          key      = "CriticalAddonsOnly"
          operator = "Exists"
          effect   = "NoSchedule"
        },
      ]

      controller = {
        resources = {
          requests = { cpu = "1", memory = "1Gi" }
          limits   = { cpu = "1", memory = "1Gi" }
        }
      }
    }),
    var.extra_values,
  ]
}

# ── EC2NodeClass: shared AMI/subnet/SG/role for all CypherX compute NodePools ──
# One EC2NodeClass for the c5/c6i compute family (same AMI + networking). Per
# Component 17b, you can split EC2NodeClasses per AMI family / constraint group;
# here a single AL2023 EKS-optimized class serves core/agent/tools.
resource "kubectl_manifest" "ec2nodeclass_compute" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata = {
      name = "cypherx-compute"
      labels = {
        "cypherx.ai/component"   = "karpenter"
        "cypherx.ai/environment" = var.environment
      }
    }
    spec = merge(local.node_identity, {
      amiFamily = "AL2023"
      amiSelectorTerms = [
        { alias = var.ami_alias },
      ]
      # Subnets + security groups discovered by the karpenter.sh/discovery tag the
      # VPC/EKS stacks stamp (set to the cluster name).
      subnetSelectorTerms = [
        { tags = { "karpenter.sh/discovery" = local.discovery_value } },
      ]
      securityGroupSelectorTerms = [
        { tags = { "karpenter.sh/discovery" = local.discovery_value } },
      ]
      # Node identity (role XOR instanceProfile) is merged from local.node_identity.
      blockDeviceMappings = [
        {
          deviceName = "/dev/xvda"
          ebs = {
            volumeSize          = var.node_volume_size
            volumeType          = "gp3"
            encrypted           = true
            deleteOnTermination = true
          }
        },
      ]
      metadataOptions = {
        httpEndpoint            = "enabled"
        httpTokens              = "required" # IMDSv2 only
        httpPutResponseHopLimit = 1
      }
      tags = {
        "karpenter.sh/discovery" = local.discovery_value
        "cypherx.ai/managed-by"  = "karpenter"
        "cypherx.ai/environment" = var.environment
      }
    })
  })

  depends_on = [helm_release.karpenter]
}

# ── NodePool: core (c5.xlarge family, on-demand) ──────────────────────────────
resource "kubectl_manifest" "nodepool_core" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata = {
      name   = "core"
      labels = { "cypherx.ai/component" = "karpenter" }
    }
    spec = {
      template = {
        metadata = {
          labels = {
            "node-role" = "core" # Component 4 node label
          }
        }
        spec = {
          nodeClassRef = {
            group = "karpenter.k8s.aws"
            kind  = "EC2NodeClass"
            name  = "cypherx-compute"
          }
          requirements = [
            # c5.xlarge family — on-demand ONLY (Component 4: core is on-demand).
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
            { key = "karpenter.k8s.aws/instance-category", operator = "In", values = ["c"] },
            { key = "karpenter.k8s.aws/instance-family", operator = "In", values = ["c5"] },
            { key = "karpenter.k8s.aws/instance-size", operator = "In", values = ["xlarge", "2xlarge"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "topology.kubernetes.io/zone", operator = "In", values = ["us-east-1a", "us-east-1b", "us-east-1c"] },
          ]
          expireAfter = "720h"
        }
      }
      disruption = {
        # core is on-demand stateless workloads — consolidation is safe.
        consolidationPolicy = "WhenEmptyOrUnderutilized"
        consolidateAfter    = "1m"
      }
      limits = {
        cpu = var.environment == "prod" ? "200" : "48"
      }
    }
  })

  depends_on = [kubectl_manifest.ec2nodeclass_compute]
}

# ── NodePool: agent (c5.2xlarge / c6i family, on-demand + spot, HPA-driven) ───
resource "kubectl_manifest" "nodepool_agent" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata = {
      name   = "agent"
      labels = { "cypherx.ai/component" = "karpenter" }
    }
    spec = {
      template = {
        metadata = {
          labels = {
            "node-role" = "agent" # Component 4 node label
          }
        }
        spec = {
          nodeClassRef = {
            group = "karpenter.k8s.aws"
            kind  = "EC2NodeClass"
            name  = "cypherx-compute"
          }
          requirements = [
            # agent: on-demand + SPOT (Component 4 — mixed, HPA-driven).
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["spot", "on-demand"] },
            { key = "karpenter.k8s.aws/instance-category", operator = "In", values = ["c"] },
            # c5.2xlarge / c6i family.
            { key = "karpenter.k8s.aws/instance-family", operator = "In", values = ["c5", "c6i"] },
            { key = "karpenter.k8s.aws/instance-size", operator = "In", values = ["2xlarge", "4xlarge"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "topology.kubernetes.io/zone", operator = "In", values = ["us-east-1a", "us-east-1b", "us-east-1c"] },
          ]
          expireAfter = "720h"
        }
      }
      disruption = {
        consolidationPolicy = "WhenEmptyOrUnderutilized"
        consolidateAfter    = "2m"
      }
      limits = {
        cpu = var.environment == "prod" ? "640" : "64"
      }
    }
  })

  depends_on = [kubectl_manifest.ec2nodeclass_compute]
}

# ── NodePool: tools (c5.large / c6i family, on-demand + spot) ─────────────────
resource "kubectl_manifest" "nodepool_tools" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata = {
      name   = "tools"
      labels = { "cypherx.ai/component" = "karpenter" }
    }
    spec = {
      template = {
        metadata = {
          labels = {
            "node-role" = "tools" # Component 4 node label
          }
        }
        spec = {
          nodeClassRef = {
            group = "karpenter.k8s.aws"
            kind  = "EC2NodeClass"
            name  = "cypherx-compute"
          }
          requirements = [
            # tools: on-demand + SPOT (Component 4).
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["spot", "on-demand"] },
            { key = "karpenter.k8s.aws/instance-category", operator = "In", values = ["c"] },
            # c5.large / c6i family.
            { key = "karpenter.k8s.aws/instance-family", operator = "In", values = ["c5", "c6i"] },
            { key = "karpenter.k8s.aws/instance-size", operator = "In", values = ["large", "xlarge"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "topology.kubernetes.io/zone", operator = "In", values = ["us-east-1a", "us-east-1b", "us-east-1c"] },
          ]
          expireAfter = "720h"
        }
      }
      disruption = {
        consolidationPolicy = "WhenEmptyOrUnderutilized"
        consolidateAfter    = "2m"
      }
      limits = {
        cpu = var.environment == "prod" ? "200" : "32"
      }
    }
  })

  depends_on = [kubectl_manifest.ec2nodeclass_compute]
}

# NOTE (NON-OVERLAP GUARD — Component 4, do NOT remove):
#   There is deliberately NO `observability` NodePool and NO `system` NodePool.
#   - observability: fixed EKS-managed NG; Prometheus/Loki/Tempo PVCs are pinned
#     and "do NOT consolidate observability" — Karpenter must not touch it.
#   - system:        fixed EKS-managed NG hosting kube-system + Karpenter itself;
#     Karpenter cannot provision its own host node.
#   Adding a NodePool for either role would overlap the managed NG and the two
#   scalers would fight. Keep these as managed node groups only.
