# cypherx-service (base Helm chart)

The CypherX **base service chart**. Every SharedCore / xAgent / tools service is
deployed through this chart so no service can drift from platform standards. It
encodes Phase 0 contracts directly into the rendered manifests.

| Contract | What the chart enforces |
|----------|-------------------------|
| **6** — Structured logs | Injects `SERVICE`, `VERSION`, `ENVIRONMENT`, `LOG_FORMAT=json` (and `LOG_LEVEL`). `logFormat` is pinned to `json` in `values.schema.json`. |
| **7** — Health/metrics | `livenessProbe` → `GET /livez` (never checks downstreams), `readinessProbe` → `GET /readyz`, container ports `http:8080` + `metrics:9090`. ServiceMonitor + NetworkPolicy scrape `:9090`. |
| **8** — Trace propagation | `OTEL_EXPORTER_OTLP_ENDPOINT` → `tempo-distributor.observability.svc.cluster.local:4317`, `OTEL_PROPAGATORS` includes `tracecontext` (W3C). |
| **13** — Tenant model | `POSTGRES_DSN` points at the **transaction-mode** PgBouncer pooler `pgbouncer.data.svc.cluster.local:6432` — required for `SET LOCAL app.tenant_id` RLS. Never RDS-direct. |
| **14** — Migrations | Atlas migration `Job` as a `pre-install,pre-upgrade` Helm hook. Job runs as the `*_ddl` user (separate DopplerSecret + dedicated SA); the runtime pod uses `*_user`. |

## What it renders

| Template | Purpose |
|----------|---------|
| `deployment.yaml` | Runtime Deployment. Non-root, `readOnlyRootFilesystem`, drops `ALL` caps; contract env; probes; topologySpreadConstraints; node-role affinity; Istio sidecar. |
| `service.yaml` | ClusterIP with `http` + `metrics` ports. |
| `serviceaccount.yaml` | Runtime SA, optional IRSA `eks.amazonaws.com/role-arn`. |
| `hpa.yaml` | HPA on CPU + memory (needs metrics-server). |
| `pdb.yaml` | PodDisruptionBudget (`minAvailable`). |
| `servicemonitor.yaml` | Prometheus Operator ServiceMonitor scraping `:metrics`. |
| `networkpolicy.yaml` | Default-deny ingress + allow from `ingress` (Kong), same-namespace, and `observability` (metrics only). |
| `migration-job.yaml` | Atlas migration Job hook + its dedicated ServiceAccount (Contract 14). |
| `dopplersecret.yaml` | `DopplerSecret` CRs: one runtime (`*-runtime`), one DDL-only (`*-ddl`). |
| `virtualservice.yaml` / `destinationrule.yaml` | Optional Istio routing, guarded by `istio.virtualService.enabled` / `istio.destinationRule.enabled`. |

## Usage

Service repos do **not** copy these templates. They depend on this chart and
supply values only — see `charts/example-service/` and `charts/README.md`.

```yaml
# Chart.yaml (in a service repo)
dependencies:
  - name: cypherx-service
    version: "0.1.0"
    repository: "oci://<registry>/cypherx-charts"   # or file://../cypherx-service
```

```bash
helm dependency build
helm lint . -f values.yaml
helm template my-svc . -f values.yaml
```

## Required per-service values

| Value | Example | Notes |
|-------|---------|-------|
| `service` | `auth-service` | Contract 6 `service` field / `cypherx.ai/service` label. |
| `image.repository` | `…/cypherx/auth-service` | ECR repo. |
| `image.tag` | `1.2.3` | Also becomes `VERSION`. |
| `nodeRole` | `core` | Phase 1 Component 5 node-role label. |
| `database.schema` / `runtimeUser` / `ddlUser` | `auth` / `auth_user` / `auth_ddl` | Phase 1 Component 14/16. |
| `doppler.runtimeConfig` | `shared-core.auth` | Holds `db/<svc>/runtime_password`. |
| `doppler.ddlConfig` | `db.auth.ddl` | Holds `db/<svc>/ddl_password` — Job only. |

## Guardrails baked into the chart (do not "fix")

- **Liveness never checks downstreams** (Contract 7). `/livez` is process-only;
  `/readyz` checks DB/Kafka. Do not point liveness at `/readyz`.
- **Metrics on :9090** — the Istio `metrics-permissive` PeerAuthentication and the
  observability scrape NetworkPolicy both assume `9090`. The schema makes it a
  `const`.
- **DSN → PgBouncer transaction mode** (Contract 13). Pointing a service at RDS
  directly breaks `SET LOCAL` tenant isolation.
- **Migration Job uses `*_ddl`, runtime uses `*_user`** (Contract 14). The DDL
  password is synced into a separate Secret mounted only into the Job, via a
  dedicated ServiceAccount with `automountServiceAccountToken: false`. The
  cluster-wide `CREATEROLE` limitation (Postgres lacks schema-scoped CREATEROLE)
  is mitigated by this isolation — see Phase 1 Component 16.
- **Migration hook delete-policy is `before-hook-creation`** (not `hook-failed`)
  so a failed migration Job is retained for log triage; Helm aborts the upgrade
  and the previous Deployment keeps serving (Contract 14 failure handling).

## Values reference

See `values.yaml` (documented inline) and `values.schema.json` (strict — unknown
keys are rejected, `additionalProperties: false` everywhere).
