# ADR-003 · Kong API Gateway + Istio Service Mesh in Cloud; Caddy in Local Compose

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX is a multi-service platform whose external-facing API surface must enforce JWT authentication, rate limiting, and request routing before traffic reaches any service. Internally, service-to-service calls must be mutually authenticated to prevent lateral movement if one service is compromised. In the cloud topology this is a standard gateway + mesh problem, but in the local development compose stack, the full Kong + Istio setup would require a Kubernetes cluster and adds substantial startup complexity. A pragmatic local alternative is needed that keeps the developer experience fast while preserving the security invariants that matter most.

## Decision

**Cloud / production:** AWS Route53 → ALB → **Kong** (JWT validation, rate limiting, routing, plugin pipeline) → **Istio** sidecar mesh (mTLS for every service-to-service call). Kong is deployed on EKS via Helm; Istio is installed as the cluster service mesh. JWT validation happens at Kong using the JWKS endpoint from `auth-service`; Istio enforces mTLS via `PeerAuthentication` policies requiring mutual TLS across the mesh.

**Local compose / development:** The entire Kong + Istio stack is replaced by a single **Caddy** reverse proxy (`edge` container, host `:8000`). Caddy terminates HTTPS locally, routes by path prefix to the correct upstream service, and adds no JWT validation itself — each service re-verifies the agent JWT against the Auth JWKS endpoint independently. There is no mTLS between containers in compose; containers communicate over a private Docker bridge network.

## Rationale

### Why This

Kong is the industry standard for Kubernetes-native API gateway needs: plugin ecosystem (rate-limit, JWT, logging, CORS, request-transform), declarative configuration via KongIngress CRDs, and native integration with AWS ALB. Pairing it with Istio provides defense-in-depth: Kong handles north-south (external → cluster) policy, Istio handles east-west (service ↔ service) mTLS, so a compromised service cannot impersonate another even within the mesh.

Replacing Kong + Istio with Caddy in local compose is a deliberate pragmatic trade-off. Running Kubernetes locally (even via kind/minikube) for a 12-service compose stack would increase cold-start time from ~30 seconds to several minutes and require every developer to manage a local cluster. Caddy is a single statically-linked binary, has an automatic HTTPS/TLS feature for local certs, and its `Caddyfile` is a 30-line reverse-proxy config. The security gap (no mTLS between containers, no Kong JWT plugin) is acceptable in a local-only private Docker network; developers exercise the JWT code paths because each service still verifies the JWT independently against Auth JWKS.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| Nginx as local edge | Less ergonomic config for path rewrites; no automatic TLS; no hot-reload without nginx -s reload signal; Caddy's Caddyfile is more readable. |
| Traefik as local edge | Good option; rejected in favor of Caddy primarily because Caddy's automatic HTTPS and simpler label-free config reduces onboarding friction. Traefik's Docker provider is powerful but its middleware chain config is more verbose for simple proxying. |
| Running Kong in Docker Compose | Kong requires a Postgres DB for configuration; adds another container, another schema, another init script; startup ordering becomes complicated. Overhead is not justified for local dev where all calls are on a trusted Docker bridge. |
| No gateway in cloud (services directly on ALB) | Loses centralized JWT validation, rate limiting, and routing policy. Each service would need to duplicate these concerns — a maintenance and security anti-pattern. |
| Envoy standalone (without Istio) | Powerful but requires significant manual xDS configuration; Istio manages Envoy sidecars declaratively and integrates with the Kubernetes control plane. Standalone Envoy at this scale is more effort than Istio. |
| AWS API Gateway | Vendor lock-in; limited JWT validation flexibility; no east-west mTLS; plugin ecosystem is AWS-only; harder to run locally. |

## Consequences

### Positive

- Cloud topology is defense-in-depth: Kong handles auth/rate-limit/routing at the perimeter, Istio handles mTLS inside the cluster — two independent enforcement points.
- Local compose stack starts in ~30 seconds with zero Kubernetes dependency; a single `docker compose up` is the full developer workflow.
- Caddy's automatic HTTPS means local development uses TLS-terminated connections that match production, catching TLS-related bugs early.
- Kong plugins (rate-limit, JWT, request-transform) are declarative and version-controlled — policy changes are GitOps PRs, not manual admin-panel changes.
- Each service independently verifying the JWT (even behind Kong) provides defense-in-depth: a misconfigured Kong plugin cannot accidentally pass unauthenticated requests to services.

### Negative / Trade-offs

- Local compose and cloud have structurally different security models. A Kong plugin misconfiguration will not be caught in local dev (Caddy does no JWT work). This is mitigated by integration tests that exercise the Auth JWKS verification path directly in each service.
- Istio sidecar injection adds ~50–100 ms to pod startup and ~10–20 MB memory per sidecar. At 12 services this is material but acceptable.
- mTLS certificate rotation is managed by Istio's internal CA (citadel); teams must understand Istio `PeerAuthentication` and `DestinationRule` resources to debug mTLS failures.
- Kong version upgrades can break plugin configuration syntax — KongIngress CRD schemas must be kept in sync with the Kong chart version in `charts/`.
- The Caddy `edge` container is a single point of failure in local compose — if it crashes, no traffic reaches any service. There is no HA in compose; this is an accepted local-dev trade-off.
