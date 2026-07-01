variable "environment" {
  description = "Environment name (dev | staging | prod). Drives the App-of-Apps target path and the dev/staging-automated vs prod-manual sync policy."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "namespace" {
  description = "Namespace ArgoCD is installed into. Component 12 pins this to 'argocd' (istio-injection disabled — bootstrapped before Istio)."
  type        = string
  default     = "argocd"
}

variable "chart_version" {
  description = "argo/argo-cd Helm chart version. Component 12 pins ArgoCD app version 2.11.x; chart 7.3.x ships ArgoCD 2.11.x."
  type        = string
  default     = "7.3.11"
}

variable "create_namespace" {
  description = "Create the argocd namespace via Helm. Set false when the namespaces module already created it (recommended — labels/injection are managed there)."
  type        = bool
  default     = false
}

variable "gitops_repo_url" {
  description = "HTTPS URL of the cypherx-gitops repository registered with ArgoCD (Component 12 / 19)."
  type        = string
  default     = "https://github.com/cypherx-ai/cypherx-gitops.git"
}

variable "gitops_repo_username" {
  description = "Username for the gitops repo HTTPS deploy credential. Sourced from Doppler (ci/gitops_deploy_username). Never hardcode."
  type        = string
  default     = "cypherx-gitops-bot"
}

variable "gitops_repo_password" {
  description = "Token/deploy-key password for the gitops repo HTTPS credential. Sourced from Doppler (ci/github_app_private_key-derived installation token). MUST come from a variable — never hardcode."
  type        = string
  sensitive   = true
}

variable "gitops_target_revision" {
  description = "Git revision (branch/tag) ArgoCD tracks for the App-of-Apps root."
  type        = string
  default     = "main"
}

variable "app_of_apps_path" {
  description = "Path in the gitops repo to the App-of-Apps manifest. Component 12/19: apps/dev-apps.yaml watches gitops/envs/<env>/ and creates child apps."
  type        = string
  default     = "apps/dev-apps.yaml"
}

variable "server_host" {
  description = "ArgoCD server hostname (argocd.<env>.cypherx.ai — internal ALB, VPN-only per Component 5). Used for the server config and ingress."
  type        = string
  default     = ""
}

variable "extra_values" {
  description = "Additional raw YAML values merged (last, highest precedence) into the argo-cd Helm release for env-specific overrides."
  type        = string
  default     = ""
}
