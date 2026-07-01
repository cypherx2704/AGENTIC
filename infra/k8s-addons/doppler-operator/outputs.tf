output "namespace" {
  description = "Namespace the Doppler operator runs in."
  value       = var.namespace
}

output "chart_version" {
  description = "Installed doppler-kubernetes-operator chart version."
  value       = helm_release.doppler_operator.version
}

output "bootstrap_token_secret_names" {
  description = "Map of K8s namespace -> bootstrap token Secret name that DopplerSecret CRs reference."
  value       = { for k, s in kubernetes_secret.namespace_bootstrap : k => s.metadata[0].name }
}

output "doppler_project" {
  description = "Doppler project the operator syncs from."
  value       = var.doppler_project
}
