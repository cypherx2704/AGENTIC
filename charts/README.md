# CypherX charts repo

Helm 3 charts for the CypherX platform. The repo's reason for existing is the
**base service chart** — every service repo consumes it so platform standards
(Phase 0 contracts) cannot drift per-service.

```
charts/
├── cypherx-service/     ← the base chart (apiVersion v2, type application)
└── example-service/     ← thin values-only consumer (lint/template target + reference)
```

## How a service repo consumes the base chart

A service repo (auth-service, llms-gateway, xagent, …) does **not** copy Helm
templates. It ships a `Chart.yaml` declaring `cypherx-service` as a dependency
and a `values.yaml` with its overrides:

```yaml
# <service-repo>/deploy/Chart.yaml
apiVersion: v2
name: auth-service
version: 0.1.0
appVersion: "1.2.3"
dependencies:
  - name: cypherx-service
    version: "0.1.0"
    repository: "oci://<registry>/cypherx-charts"     # published base chart
```

```yaml
# <service-repo>/deploy/values.yaml
cypherx-service:
  service: auth-service
  image:
    repository: <acct>.dkr.ecr.us-east-1.amazonaws.com/cypherx/auth-service
    tag: "1.2.3"
  nodeRole: core
  database:
    schema: auth
    runtimeUser: auth_user
    ddlUser: auth_ddl
  doppler:
    runtimeConfig: shared-core.auth
    ddlConfig: db.auth.ddl
```

```bash
helm dependency build
helm lint   . -f values.yaml
helm template auth . -f values.yaml | kubectl apply --dry-run=client -f -
```

ArgoCD (Phase 1 Component 12) renders the same way from the gitops repo.

## Contracts the base chart bakes in

| Contract | Enforced by |
|----------|-------------|
| **6** Structured logs | `LOG_FORMAT=json` + `SERVICE`/`VERSION`/`ENVIRONMENT` env; `logFormat` pinned to `json` in the values schema. |
| **7** Health/metrics | `livenessProbe GET /livez` (no downstream deps), `readinessProbe GET /readyz`, ports `http:8080` + `metrics:9090`, ServiceMonitor + scrape NetworkPolicy. |
| **8** Trace propagation | `OTEL_EXPORTER_OTLP_ENDPOINT` → Tempo distributor, `OTEL_PROPAGATORS=tracecontext,baggage`. |
| **13** Tenant model / RLS | `POSTGRES_DSN` → transaction-mode PgBouncer `pgbouncer.data.svc:6432`. |
| **14** Schema migrations | Atlas migration Job as a `pre-install,pre-upgrade` hook; `*_ddl` user for the Job, `*_user` for runtime; dedicated SA; DDL secret isolated. |

## Local development (no published registry)

`example-service` depends on `cypherx-service` via a relative `file://` path, so
you can lint/template the whole thing without a chart registry:

```bash
cd charts/example-service
helm dependency build
helm lint . -f values.yaml
helm template ex . -f values.yaml
```

## Versioning

- `cypherx-service` chart version is bumped (SemVer) on any template/values
  change. Service repos pin a version range.
- `appVersion` defaults the container image tag; service repos override per
  release.

## Group ownership

This directory is owned by **GROUP G6** (base Helm chart). Contracts referenced:
6, 7, 8, 13, 14. Do not add service-specific business logic here — only
platform-wide, contract-level defaults.
