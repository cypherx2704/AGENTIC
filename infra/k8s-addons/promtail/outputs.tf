output "namespace" {
  description = "Namespace the Promtail DaemonSet controller runs in."
  value       = var.namespace
}

output "chart_version" {
  description = "Installed Promtail chart version."
  value       = helm_release.promtail.version
}

output "loki_push_url" {
  description = "Loki push endpoint Promtail ships to."
  value       = local.loki_push_url
}

output "allowed_labels" {
  description = "The low-cardinality Loki label allow-list (Component 13). Smoke-test assertion #3 checks logcli labels matches this set."
  value       = local.allowed_labels
}

output "forbidden_labels" {
  description = "High-cardinality JSON fields that MUST NOT be Loki labels (queried via | json). Listed for the smoke test and reviewers."
  value       = local.forbidden_labels
}
