output "namespace_names" {
  description = "All CypherX namespaces created by this module (excludes istio-system, owned by the Istio addon)."
  value       = [for ns in kubernetes_namespace.this : ns.metadata[0].name]
}

output "injection_enabled_namespaces" {
  description = "Namespaces with istio-injection=enabled (used by the network-policies module to scope allow rules)."
  value       = [for k, v in local.namespaces : k if v.injection == "enabled"]
}

output "injection_disabled_namespaces" {
  description = "Namespaces explicitly opting out of istio sidecar injection (data, observability, argocd)."
  value       = [for k, v in local.namespaces : k if v.injection == "disabled"]
}

output "namespace_labels" {
  description = "Map of namespace name -> applied labels (for downstream NetworkPolicy podSelector/namespaceSelector matching)."
  value       = { for k, ns in kubernetes_namespace.this : k => ns.metadata[0].labels }
}
