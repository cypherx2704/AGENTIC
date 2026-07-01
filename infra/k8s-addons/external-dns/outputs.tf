output "namespace" {
  description = "Namespace external-dns is installed into."
  value       = var.namespace
}

output "chart_version" {
  description = "Pinned external-dns chart version."
  value       = var.chart_version
}

output "service_account_name" {
  description = "Name of the external-dns ServiceAccount (IRSA-bound)."
  value       = var.service_account_name
}

output "txt_owner_id" {
  description = "TXT-registry ownership id used to scope Route53 records to this env/cluster."
  value       = local.txt_owner_id
}
