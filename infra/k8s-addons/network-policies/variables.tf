variable "environment" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "namespaces" {
  description = <<-EOT
    Namespaces that receive a default deny-all-ingress NetworkPolicy. Defaults to
    the Component-6 set. `istio-system` is intentionally excluded (Istio manages
    its own policies). `messaging` is excluded (no pods to protect).
  EOT
  type        = list(string)
  default = [
    "ingress",
    "shared-core",
    "xagent",
    "tools",
    "platform-mgmt",
    "data",
    "observability",
    "argocd",
    "px0-bridge",
  ]
}

variable "observability_namespace" {
  description = "Namespace running Prometheus/Promtail that must be allowed to scrape /metrics across the cluster."
  type        = string
  default     = "observability"
}

variable "argocd_namespace" {
  description = "Namespace running the ArgoCD control plane that must be allowed to deploy/sync into workload namespaces."
  type        = string
  default     = "argocd"
}

variable "metrics_port" {
  description = "App /metrics port convention (Component 7 / Component 13). Prometheus scrape allow rule targets this port."
  type        = number
  default     = 9090
}

variable "enable_explicit_allows" {
  description = <<-EOT
    When true, render the explicit-allow NetworkPolicies (observability scrape,
    argocd deploy) in addition to the default deny-all. Left false in Phase 1
    first-cycle (deny-all only); the allows are placeholders per Component 6/7
    ("explicit allow rules per namespace defined in k8s-addons/network-policies/").
  EOT
  type        = bool
  default     = false
}
