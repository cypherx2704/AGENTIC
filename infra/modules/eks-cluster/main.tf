###############################################################################
# EKS cluster — Component 4 (phase-01-infrastructure.md, lines 196-250)
#
# Wraps terraform-aws-modules/eks/aws ~> 20.
#
# Locked-in guards (do NOT "fix"):
#   - API server is PRIVATE ONLY (public endpoint disabled). GitHub-hosted-runner
#     IP allow-listing is FORBIDDEN; CI uses in-VPC self-hosted runners + IRSA.
#   - Managed node groups are ONLY system-nodes and observability. core/agent/tools
#     are provisioned + consolidated by Karpenter (G5 / Component 17b). Adding a
#     managed NG for those roles makes the two scalers fight. Non-overlap is mandatory.
#   - system-nodes carries taint CriticalAddonsOnly=true:NoSchedule so non-system
#     pods never land there.
###############################################################################

locals {
  cluster_name = coalesce(var.cluster_name, "cypherx-${var.env}")

  # node-role label convention (Component 4, lines 241-247).
  system_labels        = { "node-role" = "system" }
  observability_labels = { "node-role" = "observability" }

  base_tags = merge(
    {
      "Environment"                                 = var.env
      "ManagedBy"                                   = "terraform"
      "Component"                                   = "eks-cluster"
      "kubernetes.io/cluster/${local.cluster_name}" = "owned"
    },
    var.tags,
  )
}

###############################################################################
# Optional dedicated KMS key for EKS secrets envelope encryption.
###############################################################################

resource "aws_kms_key" "eks" {
  count = var.kms_key_arn == null ? 1 : 0

  description             = "EKS secrets envelope encryption for ${local.cluster_name}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.base_tags
}

resource "aws_kms_alias" "eks" {
  count = var.kms_key_arn == null ? 1 : 0

  name          = "alias/${local.cluster_name}-eks"
  target_key_id = aws_kms_key.eks[0].key_id
}

locals {
  eks_kms_key_arn = var.kms_key_arn != null ? var.kms_key_arn : aws_kms_key.eks[0].arn
}

###############################################################################
# EKS cluster + managed node groups + managed add-ons.
###############################################################################

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.cluster_name
  cluster_version = var.kubernetes_version

  # --- API server access: PRIVATE ONLY (Component 4) -------------------------
  cluster_endpoint_private_access      = var.endpoint_private_access
  cluster_endpoint_public_access       = var.endpoint_public_access
  cluster_endpoint_public_access_cidrs = var.public_access_cidrs

  # --- OIDC / IRSA -----------------------------------------------------------
  enable_irsa = true

  # --- Control-plane logging -> CloudWatch (Component 4) ----------------------
  cluster_enabled_log_types              = var.enabled_cluster_log_types
  cloudwatch_log_group_retention_in_days = var.cloudwatch_log_retention_days

  # --- Networking ------------------------------------------------------------
  vpc_id                   = var.vpc_id
  subnet_ids               = var.private_subnet_ids
  control_plane_subnet_ids = var.private_subnet_ids

  cluster_additional_security_group_ids = var.cluster_security_group_ids

  # --- Secrets envelope encryption (KMS) -------------------------------------
  create_kms_key = false
  cluster_encryption_config = {
    provider_key_arn = local.eks_kms_key_arn
    resources        = ["secrets"]
  }

  # --- Managed add-ons (Component 4: kube-proxy, vpc-cni, coredns) ------------
  # coredns is created after node groups exist so its pods can schedule.
  cluster_addons = {
    kube-proxy = {
      addon_version               = try(var.addon_versions.kube_proxy, null)
      resolve_conflicts_on_update = "OVERWRITE"
    }
    vpc-cni = {
      addon_version               = try(var.addon_versions.vpc_cni, null)
      resolve_conflicts_on_update = "OVERWRITE"
      before_compute              = true
    }
    coredns = {
      addon_version               = try(var.addon_versions.coredns, null)
      resolve_conflicts_on_update = "OVERWRITE"
      most_recent                 = try(var.addon_versions.coredns, null) == null
    }
  }

  # --- Managed node groups: system-nodes + observability ONLY ----------------
  eks_managed_node_groups = {
    system-nodes = {
      ami_type       = "AL2_x86_64"
      instance_types = var.system_node_group.instance_types
      capacity_type  = var.node_capacity_type

      desired_size = var.system_node_group.desired_size
      min_size     = var.system_node_group.min_size
      max_size     = var.system_node_group.max_size

      disk_size = var.system_node_group.disk_size_gb

      # Attach the shared sg-eks-nodes (from the vpc module) alongside the EKS-managed
      # node SG so SG rules in Component 3 (sg-kong -> sg-eks-nodes, etc.) apply.
      vpc_security_group_ids = var.node_security_group_ids

      labels = local.system_labels

      # Prevent non-system pods landing on system nodes (Component 4, line 249).
      taints = {
        critical-addons-only = {
          key    = "CriticalAddonsOnly"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      }

      tags = merge(local.base_tags, { "node-role" = "system" })
    }

    observability = {
      ami_type       = "AL2_x86_64"
      instance_types = var.observability_node_group.instance_types
      capacity_type  = var.node_capacity_type

      desired_size = var.observability_node_group.desired_size
      min_size     = var.observability_node_group.min_size
      max_size     = var.observability_node_group.max_size

      disk_size = var.observability_node_group.disk_size_gb

      vpc_security_group_ids = var.node_security_group_ids

      # No taint: observability workloads tolerate nothing special, just land here
      # via the node-role=observability nodeSelector. Karpenter must NOT consolidate
      # these nodes (Prometheus/Loki EBS PVCs are pinned) — enforced in the Karpenter
      # disruption budget (G5), not here.
      labels = local.observability_labels

      tags = merge(local.base_tags, { "node-role" = "observability" })
    }
  }

  tags = local.base_tags
}
