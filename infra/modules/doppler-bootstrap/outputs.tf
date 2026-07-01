# ---------------------------------------------------------------------------------------------------------------------
# modules/doppler-bootstrap/outputs.tf — Components 11 + 20.
# ---------------------------------------------------------------------------------------------------------------------

output "project_name" {
  description = "The Doppler project."
  value       = doppler_project.platform.name
}

output "config_name" {
  description = "The per-env Doppler config (dev|staging|prod)."
  value       = doppler_config.env.name
}

output "secret_paths" {
  description = "All mandatory secret paths bootstrapped as placeholders (Component 20)."
  value       = sort(keys(local.all_secret_paths))
}

output "operator_service_tokens" {
  description = <<-EOT
    Per-(env,namespace) Doppler operator service tokens (Component 11). SENSITIVE — consumed by the
    operator-bootstrap stack to seed the Doppler K8s operator Secret. Never log or commit these.
  EOT
  value       = { for ns, t in doppler_service_token.operator : ns => t.key }
  sensitive   = true
}
