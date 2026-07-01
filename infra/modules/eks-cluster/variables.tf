###############################################################################
# EKS cluster module — input variables
#
# Component 4 (phase-01-infrastructure.md):
#   - One cluster per environment (cypherx-<env>).
#   - PRIVATE-ONLY API server (public endpoint disabled).
#   - OIDC/IRSA enabled.
#   - Control-plane logging: api, audit, authenticator -> CloudWatch.
#   - Managed add-ons: kube-proxy, vpc-cni, coredns.
#   - Managed node groups: system-nodes + observability ONLY.
#     core/agent/tools are owned by Karpenter (G5) — do NOT add them here.
###############################################################################

variable "env" {
  description = "Environment name (dev | staging | prod). Drives the cluster name cypherx-<env>."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "cluster_name" {
  description = "Optional explicit cluster name. Defaults to cypherx-<env> when null."
  type        = string
  default     = null
}

variable "kubernetes_version" {
  description = "EKS Kubernetes control-plane version. Component 4 pins 1.30."
  type        = string
  default     = "1.30"
}

variable "vpc_id" {
  description = "VPC ID the cluster is deployed into (from the vpc module/G1)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs (3 AZs) for the control plane ENIs and managed node groups."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "At least two private subnets (distinct AZs) are required for EKS."
  }
}

variable "node_security_group_ids" {
  description = "Additional security group IDs (e.g. sg-eks-nodes from the vpc module) attached to all managed nodes."
  type        = list(string)
  default     = []
}

variable "cluster_security_group_ids" {
  description = "Additional security group IDs attached to the EKS control-plane ENIs."
  type        = list(string)
  default     = []
}

# --- API server access (PRIVATE ONLY per Component 4) -------------------------

variable "endpoint_private_access" {
  description = "Enable the private API endpoint. MUST stay true — developers/CI reach the API via VPN / in-VPC runners."
  type        = bool
  default     = true
}

variable "endpoint_public_access" {
  description = "Enable the PUBLIC API endpoint. MUST stay false (Component 4: PRIVATE ONLY). GitHub-hosted-runner IP allow-listing is FORBIDDEN."
  type        = bool
  default     = false
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to the public endpoint. Only consulted if endpoint_public_access is (incorrectly) enabled."
  type        = list(string)
  default     = []
}

# --- Control-plane logging (Component 4) -------------------------------------

variable "enabled_cluster_log_types" {
  description = "Control-plane log types shipped to CloudWatch. Component 4 requires api, audit, authenticator."
  type        = list(string)
  default     = ["api", "audit", "authenticator"]
}

variable "cloudwatch_log_retention_days" {
  description = "Retention (days) for the /aws/eks/<cluster>/cluster CloudWatch log group."
  type        = number
  default     = 90
}

# --- Encryption --------------------------------------------------------------

variable "kms_key_arn" {
  description = "Optional KMS key ARN for EKS secrets envelope encryption. When null, a dedicated key is created."
  type        = string
  default     = null
}

# --- Managed add-on versions (null => AWS-resolved default for the K8s ver) ---

variable "addon_versions" {
  description = "Optional pinned versions for the AWS-managed add-ons. null lets EKS pick the default for the K8s version."
  type = object({
    coredns    = optional(string)
    kube_proxy = optional(string)
    vpc_cni    = optional(string)
  })
  default = {}
}

# --- Managed node groups -----------------------------------------------------
# Only system-nodes and observability. core/agent/tools => Karpenter (G5).

variable "system_node_group" {
  description = "system-nodes managed node group. Hosts kube-system + Karpenter itself."
  type = object({
    instance_types = optional(list(string), ["t3.medium"])
    desired_size   = optional(number, 3)
    min_size       = optional(number, 3)
    max_size       = optional(number, 3)
    disk_size_gb   = optional(number, 50)
  })
  default = {}
}

variable "observability_node_group" {
  description = "observability managed node group. Pinned (Prometheus/Loki EBS PVCs); never consolidated by Karpenter."
  type = object({
    instance_types = optional(list(string), ["m5.large"])
    desired_size   = optional(number, 2)
    min_size       = optional(number, 2)
    max_size       = optional(number, 2)
    disk_size_gb   = optional(number, 100)
  })
  default = {}
}

variable "node_capacity_type" {
  description = "Capacity type for managed node groups. Component 4: ON_DEMAND (static, never spot)."
  type        = string
  default     = "ON_DEMAND"

  validation {
    condition     = var.node_capacity_type == "ON_DEMAND"
    error_message = "Managed node groups in Component 4 are ON_DEMAND only; spot is reserved for Karpenter agent/tools NodePools."
  }
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
