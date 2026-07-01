output "default_deny_policy_names" {
  description = "Map of namespace -> default-deny-ingress NetworkPolicy name."
  value       = { for k, np in kubernetes_network_policy.default_deny_ingress : k => np.metadata[0].name }
}

output "guarded_namespaces" {
  description = "Namespaces with a default-deny-ingress baseline applied."
  value       = sort(tolist(local.guarded_namespaces))
}

output "explicit_allows_enabled" {
  description = "Whether the observability-scrape and argocd-deploy explicit-allow policies are rendered."
  value       = var.enable_explicit_allows
}
