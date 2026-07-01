# Components 1 & 10 — AWS Account & IAM.
#
# Role split (from Component 1 "Note on the IAM separation-of-duty boundary"):
#   GitHubActionsRole   — GitHub OIDC trust; ECR push + S3 state read + EKS describe; CANNOT touch IAM.
#   TerraformInfraRole  — VPC/EKS/RDS/MSK/ElastiCache/ECR/Route53; CANNOT create/modify IAM.
#   TerraformIAMRole    — IAM only (roles, IRSA mappings, policy attachments); used by the
#                         environments/<env>/iam/ stack; PRs require a SECOND human approver
#                         (CODEOWNERS — see README).
#   EKS Node Role       — EC2 trust; ECR pull only (IRSA base + worker node baseline).
#   AWSLoadBalancerControllerRole — IRSA; ec2:*, elasticloadbalancing:*, iam:CreateServiceLinkedRole.
#
# Guard (do NOT remove): neither Terraform role may modify itself, GitHubActionsRole,
# or any role tagged protected=true. Enforced by an explicit Deny on the iam:* /
# self-mutating verbs over the managed-role ARNs + a protected-role condition.

# ===========================================================================
# GitHub OIDC provider (account-global; one stack owns it)
# ===========================================================================

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub Actions OIDC thumbprint. GitHub uses a publicly trusted CA; AWS no
  # longer hard-validates the thumbprint, but the field is still required.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = local.common_tags
}

# ===========================================================================
# GitHubActionsRole — OIDC, no IAM
# ===========================================================================

data "aws_iam_policy_document" "github_actions_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.github_allowed_repos
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name                 = "${local.prefix}-github-actions"
  assume_role_policy   = data.aws_iam_policy_document.github_actions_trust.json
  max_session_duration = 3600

  tags = merge(local.common_tags, {
    Role = "GitHubActionsRole"
  })
}

data "aws_iam_policy_document" "github_actions" {
  # ECR push (build + push service images).
  statement {
    sid    = "EcrAuth"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPush"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeRepositories",
      "ecr:ListImages",
    ]
    resources = ["arn:aws:ecr:${var.region}:${var.account_id}:repository/cypherx/*"]
  }

  # S3 read on Terraform state (CI runs `terraform plan` read-only against state).
  statement {
    sid    = "StateRead"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      var.state_bucket_arn,
      "${var.state_bucket_arn}/*",
    ]
  }

  # EKS describe (CI resolves cluster endpoint/CA to talk to the in-VPC runner).
  statement {
    sid    = "EksDescribe"
    effect = "Allow"
    actions = [
      "eks:DescribeCluster",
      "eks:ListClusters",
    ]
    resources = ["arn:aws:eks:${var.region}:${var.account_id}:cluster/${var.name_prefix}-*"]
  }

  # Component 18 — scoped Secrets Manager read for CI bootstrap secrets only.
  statement {
    sid    = "CiSecretsRead"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = ["arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:cypherx/ci/*"]
  }

  # Boundary: GitHubActionsRole can NEVER touch IAM. Explicit deny beats any
  # future accidental allow-grant.
  statement {
    sid       = "DenyIam"
    effect    = "Deny"
    actions   = ["iam:*", "sts:AssumeRole"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  name   = "${local.prefix}-github-actions"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions.json
}

# ===========================================================================
# Shared self-protection guard for the Terraform roles
# ===========================================================================

# Deny statement injected into BOTH Terraform role policies: they may not mutate
# themselves, GitHubActionsRole, the IAM provider, or any role tagged
# protected=true. This is the verbatim Component 1 guard.
data "aws_iam_policy_document" "role_self_protection" {
  statement {
    sid    = "DenyMutateManagedAndProtectedRoles"
    effect = "Deny"
    actions = [
      "iam:UpdateAssumeRolePolicy",
      "iam:DeleteRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:UpdateRole",
      "iam:PutRolePermissionsBoundary",
      "iam:DeleteRolePermissionsBoundary",
      "iam:TagRole",
      "iam:UntagRole",
    ]
    resources = local.managed_role_arns
  }

  statement {
    sid    = "DenyMutateProtectedTaggedRoles"
    effect = "Deny"
    actions = [
      "iam:UpdateAssumeRolePolicy",
      "iam:DeleteRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:UpdateRole",
      "iam:PutRolePermissionsBoundary",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/protected"
      values   = ["true"]
    }
  }
}

# ===========================================================================
# TerraformInfraRole — infra, NO IAM
# ===========================================================================

data "aws_iam_policy_document" "terraform_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = length(var.terraform_trusted_principal_arns) > 0 ? var.terraform_trusted_principal_arns : ["arn:aws:iam::${var.account_id}:root"]
    }

    dynamic "condition" {
      for_each = var.require_mfa_for_terraform ? [1] : []
      content {
        test     = "Bool"
        variable = "aws:MultiFactorAuthPresent"
        values   = ["true"]
      }
    }
  }
}

resource "aws_iam_role" "terraform_infra" {
  name                 = "${local.prefix}-terraform-infra"
  assume_role_policy   = data.aws_iam_policy_document.terraform_trust.json
  max_session_duration = 3600

  tags = merge(local.common_tags, {
    Role = "TerraformInfraRole"
  })
}

data "aws_iam_policy_document" "terraform_infra" {
  # Infra services this role may fully manage (Component 1).
  statement {
    sid    = "InfraServices"
    effect = "Allow"
    actions = [
      "ec2:*",         # VPC, subnets, SGs, NAT, IGW, route tables
      "eks:*",         # EKS clusters, node groups, addons
      "rds:*",         # PostgreSQL
      "kafka:*",       # MSK
      "elasticache:*", # Valkey
      "ecr:*",         # ECR repos
      "route53:*",     # DNS
      "acm:*",         # certificates
      "elasticloadbalancing:*",
      "logs:*", # CloudWatch log groups for EKS control-plane logs
      "kms:CreateKey",
      "kms:CreateAlias",
      "kms:DescribeKey",
      "kms:ListAliases",
      "kms:TagResource",
      "kms:PutKeyPolicy",
      "kms:ScheduleKeyDeletion",
      "kms:EnableKeyRotation",
      "autoscaling:*", # node-group ASGs
      "s3:*",          # loki/tempo buckets, state backend
      "dynamodb:*",    # state lock table
    ]
    resources = ["*"]
  }

  # EKS clusters & some addons need service-linked roles + iam:PassRole to hand
  # the (already-created) node role to the node group. PassRole is the ONLY iam
  # verb this role gets, and only for the node/lbc roles managed by TerraformIAMRole.
  statement {
    sid    = "PassNodeRoles"
    effect = "Allow"
    actions = [
      "iam:PassRole",
      "iam:GetRole",
      "iam:GetInstanceProfile",
    ]
    resources = [
      "arn:aws:iam::${var.account_id}:role/${local.prefix}-eks-node",
      "arn:aws:iam::${var.account_id}:role/${local.prefix}-aws-lbc",
      "arn:aws:iam::${var.account_id}:instance-profile/${local.prefix}-*",
    ]
  }

  statement {
    sid    = "ServiceLinkedRoles"
    effect = "Allow"
    actions = [
      "iam:CreateServiceLinkedRole",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values = [
        "eks.amazonaws.com",
        "eks-nodegroup.amazonaws.com",
        "elasticache.amazonaws.com",
        "rds.amazonaws.com",
        "kafka.amazonaws.com",
        "elasticloadbalancing.amazonaws.com",
      ]
    }
  }

  # Boundary: cannot create/modify IAM roles, users, policies (separation of
  # duty). PassRole above is explicitly NOT covered by this deny because it is
  # scoped to pre-existing node/lbc roles and is read-only with respect to IAM.
  statement {
    sid    = "DenyIamMutation"
    effect = "Deny"
    actions = [
      "iam:CreateRole",
      "iam:CreateUser",
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
      "iam:AttachRolePolicy",
      "iam:AttachUserPolicy",
      "iam:PutRolePolicy",
      "iam:PutUserPolicy",
      "iam:UpdateAssumeRolePolicy",
      "iam:DeleteRole",
      "iam:DeleteUser",
      "iam:DeletePolicy",
      "iam:CreateOpenIDConnectProvider",
      "iam:DeleteOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "terraform_infra" {
  name   = "${local.prefix}-terraform-infra"
  role   = aws_iam_role.terraform_infra.id
  policy = data.aws_iam_policy_document.terraform_infra.json
}

resource "aws_iam_role_policy" "terraform_infra_self_protection" {
  name   = "${local.prefix}-self-protection"
  role   = aws_iam_role.terraform_infra.id
  policy = data.aws_iam_policy_document.role_self_protection.json
}

# ===========================================================================
# TerraformIAMRole — IAM ONLY (second approver via CODEOWNERS, see README)
# ===========================================================================

resource "aws_iam_role" "terraform_iam" {
  name                 = "${local.prefix}-terraform-iam"
  assume_role_policy   = data.aws_iam_policy_document.terraform_trust.json
  max_session_duration = 3600

  tags = merge(local.common_tags, {
    Role = "TerraformIAMRole"
  })
}

data "aws_iam_policy_document" "terraform_iam" {
  # IAM management only — roles, policies, IRSA mappings, OIDC providers.
  statement {
    sid    = "IamManagement"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:ListRoles",
      "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:CreatePolicy",
      "iam:DeletePolicy",
      "iam:GetPolicy",
      "iam:ListPolicies",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion",
      "iam:GetPolicyVersion",
      "iam:ListPolicyVersions",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:CreateInstanceProfile",
      "iam:DeleteInstanceProfile",
      "iam:AddRoleToInstanceProfile",
      "iam:RemoveRoleFromInstanceProfile",
      "iam:CreateOpenIDConnectProvider",
      "iam:DeleteOpenIDConnectProvider",
      "iam:GetOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint",
      "iam:TagOpenIDConnectProvider",
      "iam:PutRolePermissionsBoundary",
      "iam:DeleteRolePermissionsBoundary",
    ]
    resources = ["*"]
  }

  # Boundary: cannot provision infrastructure (separation of duty — mirror of
  # TerraformInfraRole). Keeps the IAM stack strictly identity-scoped.
  statement {
    sid    = "DenyInfra"
    effect = "Deny"
    actions = [
      "ec2:*",
      "eks:*",
      "rds:*",
      "kafka:*",
      "elasticache:*",
      "route53:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "terraform_iam" {
  name   = "${local.prefix}-terraform-iam"
  role   = aws_iam_role.terraform_iam.id
  policy = data.aws_iam_policy_document.terraform_iam.json
}

resource "aws_iam_role_policy" "terraform_iam_self_protection" {
  name   = "${local.prefix}-self-protection"
  role   = aws_iam_role.terraform_iam.id
  policy = data.aws_iam_policy_document.role_self_protection.json
}

# ===========================================================================
# EKS Node Role (IRSA base) — EC2 trust, ECR pull only
# ===========================================================================

data "aws_iam_policy_document" "eks_node_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_node" {
  name               = "${local.prefix}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.eks_node_trust.json

  tags = merge(local.common_tags, {
    Role = "EKSNodeRole"
  })
}

# Baseline worker node managed policies.
resource "aws_iam_role_policy_attachment" "eks_node_worker" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_node_cni" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

# ECR pull only (Component 1: "scoped to ECR pull only").
resource "aws_iam_role_policy_attachment" "eks_node_ecr_pull" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# ===========================================================================
# AWSLoadBalancerControllerRole (IRSA) — Component 10
# ===========================================================================

data "aws_iam_policy_document" "aws_lbc_trust" {
  count = local.irsa_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = ["system:serviceaccount:${var.aws_lbc_namespace}:${var.aws_lbc_service_account}"]
    }
  }
}

resource "aws_iam_role" "aws_lbc" {
  count              = local.irsa_enabled ? 1 : 0
  name               = "${local.prefix}-aws-lbc"
  assume_role_policy = data.aws_iam_policy_document.aws_lbc_trust[0].json

  tags = merge(local.common_tags, {
    Role = "AWSLoadBalancerControllerRole"
  })
}

# Permissions per Component 10: ec2:*, elasticloadbalancing:*,
# iam:CreateServiceLinkedRole. Kept to the controller's documented scope.
data "aws_iam_policy_document" "aws_lbc" {
  count = local.irsa_enabled ? 1 : 0

  statement {
    sid    = "Ec2AndElb"
    effect = "Allow"
    actions = [
      "ec2:*",
      "elasticloadbalancing:*",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "CreateServiceLinkedRole"
    effect    = "Allow"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["elasticloadbalancing.amazonaws.com"]
    }
  }

  statement {
    sid    = "WafAndShieldAndCerts"
    effect = "Allow"
    actions = [
      "acm:ListCertificates",
      "acm:DescribeCertificate",
      "wafv2:GetWebACL",
      "wafv2:GetWebACLForResource",
      "wafv2:AssociateWebACL",
      "wafv2:DisassociateWebACL",
      "shield:GetSubscriptionState",
      "shield:DescribeProtection",
      "shield:CreateProtection",
      "shield:DeleteProtection",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "aws_lbc" {
  count  = local.irsa_enabled ? 1 : 0
  name   = "${local.prefix}-aws-lbc"
  role   = aws_iam_role.aws_lbc[0].id
  policy = data.aws_iam_policy_document.aws_lbc[0].json
}
