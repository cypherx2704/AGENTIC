output "namespace" {
  description = "Namespace ArgoCD is installed into."
  value       = var.namespace
}

output "chart_version" {
  description = "Installed argo-cd Helm chart version."
  value       = helm_release.argocd.version
}

output "server_host" {
  description = "ArgoCD server hostname (internal ALB / VPN-only)."
  value       = local.server_host
}

output "app_of_apps_name" {
  description = "Name of the App-of-Apps root Application."
  value       = "cypherx-platform"
}

output "sync_policy" {
  description = "Effective sync policy for this environment (automated for dev/staging, manual for prod)."
  value       = local.automated_sync ? "automated-selfheal-prune" : "manual"
}

output "gitops_repo_url" {
  description = "Registered gitops repository URL."
  value       = var.gitops_repo_url
}
