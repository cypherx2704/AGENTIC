output "namespace" {
  description = "Namespace Kong is installed into."
  value       = var.namespace
}

output "kong_chart_version" {
  description = "Pinned kong/kong chart version."
  value       = var.kong_chart_version
}

output "proxy_service_name" {
  description = "Name of the Kong proxy LoadBalancer Service (the one the ALB fronts)."
  value       = "kong-kong-proxy"
}

output "route_map" {
  description = "Authoritative /v1/* path -> backend service map (consumed by service charts in phases 2–9)."
  value       = local.route_map
}
