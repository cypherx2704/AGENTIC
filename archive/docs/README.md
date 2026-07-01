# CypherX AI Platform — Technical Documentation

> Multi-tenant, contract-first, microservices AI agent platform.
> All diagrams are Mermaid source — render in any Mermaid-compatible viewer (GitHub, GitLab, VS Code extension, mermaid.live).

## Reading Order

For first-time readers, follow the table left-to-right: overview → architecture → domain → workflows → services → operations.

| Section | What you learn |
|---------|---------------|
| [01 · Project Overview](01-project-overview/README.md) | What the platform is, key features, tech stack, high-level diagram |
| [02 · Requirements](02-requirements/README.md) | Functional & non-functional requirements, constraints |
| [03 · Architecture](03-architecture/README.md) | System context, component, service-interaction, deployment, data-flow, event-flow, sequence diagrams |
| [04 · Domain Model](04-domain-model/README.md) | Core entities, relationships, ER diagram, business rules |
| [05 · Workflows](05-workflows/README.md) | End-to-end user flows with use-case and sequence diagrams |
| [06 · Services](06-services/README.md) | Deep-dive per service: responsibilities, APIs, DB tables, events, failure modes |
| [07 · API](07-api/README.md) | API standards, authentication, error format, every endpoint |
| [08 · Database](08-database/README.md) | All schemas, tables, indexes, migrations, ER diagrams |
| [09 · Security](09-security/README.md) | JWT design, RBAC, RLS, encryption, secrets, threat model |
| [10 · Infrastructure](10-infrastructure/README.md) | Kubernetes, networking, CI/CD, env setup, disaster recovery |
| [11 · Observability](11-observability/README.md) | Logging, metrics, tracing, dashboards, alerts, SLOs |
| [12 · Testing](12-testing/README.md) | Unit, integration, contract, E2E, load, security tests |
| [13 · Runbooks](13-runbooks/README.md) | Incident playbooks for every failure mode |
| [14 · Developer Guide](14-developer-guide/README.md) | Local setup, coding standards, how-to guides, contribution |
| [15 · ADRs](15-adrs/README.md) | Architecture Decision Records |

## Diagrams Index

All Mermaid source files live in [diagrams/](diagrams/). Each section embeds its diagrams inline.

| Diagram | Location | Used in |
|---------|----------|---------|
| System Context | `diagrams/context/system-context.md` | §03 |
| High-Level Architecture | `diagrams/architecture/high-level.md` | §01, §03 |
| Component Decomposition | `diagrams/components/platform-components.md` | §03 |
| Service Interaction | `diagrams/services/service-interaction.md` | §03 |
| Deployment (Compose) | `diagrams/deployment/compose-stack.md` | §03, §10 |
| Deployment (Cloud/K8s) | `diagrams/deployment/cloud-k8s.md` | §03, §10 |
| Data Flow | `diagrams/architecture/data-flow.md` | §03 |
| Event Flow (Kafka) | `diagrams/architecture/event-flow.md` | §03 |
| Login Sequence | `diagrams/sequence/login.md` | §05 |
| Agent Task Sequence | `diagrams/sequence/agent-task.md` | §05 |
| JWT Issuance Sequence | `diagrams/sequence/jwt-issuance.md` | §05 |
| Guardrail Check Sequence | `diagrams/sequence/guardrail-check.md` | §05 |
| ER Diagram | `diagrams/database/er-diagram.md` | §04, §08 |
| Security Architecture | `diagrams/security/security-arch.md` | §09 |
| Network Topology | `diagrams/network/network-topology.md` | §10 |

## Quick Reference

- **Local dev:** `cd infra/compose && docker compose up -d --build`
- **Single entrypoint:** `http://localhost:8000` (Caddy edge proxy)
- **Admin console:** `http://localhost:3000`
- **Auth JWKS:** `http://localhost:8080/.well-known/jwks.json`
- **Contracts:** `contracts/` — JSON Schema / OpenAPI source of truth
- **Phase specs:** `archive/Manoj/phases/`
- **Amendments:** `archive/Manoj/phases/amendments/plan-fixes.json` (overrides phase docs)
