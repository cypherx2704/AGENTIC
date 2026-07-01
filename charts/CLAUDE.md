# CLAUDE.md — charts

> The CypherX **base Helm chart** repo: `cypherx-service` (the single base chart every service
> deploys through) + `example-service` (a values-only reference consumer). It bakes Phase 0
> contracts into rendered Kubernetes manifests so no service repo can drift from platform
> standards. Platform root guide: [../CLAUDE.md](../CLAUDE.md).

## What this is
Helm 3 chart library for the CypherX platform, owned by **GROUP G6** (base Helm chart). Two charts:
`cypherx-service/` (the base `application` chart — the reason this repo exists) and
`example-service/` (a thin, templates-free consumer that depends on the base chart via a relative
`file://` path for lint/template CI and as the copy-me reference for new service repos).
Implements parts of **Phase 1 — Infrastructure** (Components 5/7/11/14/16/20) and encodes
**Contracts 6, 7, 8, 13, 14**. **Status: implemented** — all base-chart templates, a strict
`values.schema.json`, and a working example consumer are present. The repo has a single commit
(`Phase 1 — cypherx-service base Helm chart`); `development` and `feature/base-implementation`
point at the same commit. No Chart.lock / vendored subchart is committed (`helm dependency build`
is run by the consumer at deploy time).

## Tech stack
- **Helm 3** charts only (`apiVersion: v2`, `type: application`). No application code, no Dockerfile —
  this repo renders Kubernetes manifests. `kubeVersion: >=1.28.0-0`.
- **values.schema.json** (JSON Schema draft-07, strict `additionalProperties:false` throughout)
  validates every consumer's values at `helm lint`/`template` time.
- Renders/assumes these cluster operators & CRDs: **Istio** (sidecar inject, VirtualService/
  DestinationRule `networking.istio.io/v1`), **Prometheus Operator** (`ServiceMonitor`
  `monitoring.coreos.com/v1`), **Doppler Kubernetes Operator** (`DopplerSecret`
  `secrets.doppler.com/v1alpha1`), HPA (`autoscaling/v2`), PDB (`policy/v1`), NetworkPolicy.
- **Atlas** (`arigaio/atlas:0.31.0`) image for the migration Job.

## Repository layout
| Path | Holds |
|------|-------|
| `README.md` | How a service repo consumes the base chart; contract table; local `file://` flow. |
| `cypherx-service/Chart.yaml` | Base chart metadata; `version 0.1.0`, annotation `cypherx.ai/contracts: "6,7,8,13,14"`. |
| `cypherx-service/values.yaml` | Documented defaults; every key encodes a Phase 0/1 contract. |
| `cypherx-service/values.schema.json` | Strict schema; encodes invariants as `const`/`enum`/`pattern`. |
| `cypherx-service/templates/deployment.yaml` | Runtime Deployment: non-root, RO rootfs, drop-ALL caps, contract env, probes, topology spread, node-role affinity, runtime DSN assembly. |
| `cypherx-service/templates/migration-job.yaml` | Atlas migration Job + dedicated SA, as a pre-install/pre-upgrade hook (Contract 14). |
| `cypherx-service/templates/dopplersecret.yaml` | `DopplerSecret` CRs: `*-runtime` (pod) + `*-ddl` (Job only). |
| `cypherx-service/templates/service.yaml` | ClusterIP, `http` + `metrics` ports. |
| `cypherx-service/templates/{serviceaccount,hpa,pdb,servicemonitor,networkpolicy}.yaml` | Runtime SA (IRSA opt), HPA (CPU+mem), PDB, ServiceMonitor (:metrics), default-deny NetworkPolicy. |
| `cypherx-service/templates/{virtualservice,destinationrule}.yaml` | Optional Istio routing (guarded, default off). |
| `cypherx-service/templates/_helpers.tpl` | Naming/label/secret-name template funcs. |
| `cypherx-service/templates/NOTES.txt` | Post-install summary + sidecar/schema warnings. |
| `example-service/` | Values-only consumer: `Chart.yaml` (deps `file://../cypherx-service`), `values.yaml`, `values-prod.yaml`, `README.md`. No `templates/`. |

## Build, test, run
No app to run; you lint/render charts. Run from `charts/example-service` — it is the standalone
render target (the base chart's `values.yaml` defaults are intentionally incomplete and won't lint
on their own):
```bash
cd charts/example-service
helm dependency build                  # pulls cypherx-service from file://../cypherx-service
helm lint     . -f values.yaml         # validates against values.schema.json
helm template ex . -f values.yaml
helm template ex . -f values.yaml -f values-prod.yaml      # prod overlay
helm template ex . -f values.yaml | kubectl apply --dry-run=client -f -
```
A real service repo ships its own `deploy/Chart.yaml` depending on `cypherx-service`
(`oci://<registry>/cypherx-charts` in CI, or `file://` locally) plus a `values.yaml` of overrides;
ArgoCD (Phase 1 Component 12) renders identically from the gitops repo.
- **In-container port:** `http: 8080` (Kong routes here). **Metrics:** `9090` (fixed).
- **Health:** liveness `GET /livez` (process-only), readiness `GET /readyz` (checks downstreams),
  optional startup `GET /livez`. Metrics scraped at `:9090/metrics`.
- This chart has **no entry in infra/compose** — it targets Kubernetes, not the local Compose stack.

## Configuration & secrets
The chart **injects** env into the rendered Deployment (the service binary reads them):
- Contract 6: `SERVICE`, `VERSION` (= image tag, default `.Chart.AppVersion`), `ENVIRONMENT`,
  `LOG_FORMAT=json`, `LOG_LEVEL`.
- Contract 8: `OTEL_EXPORTER_OTLP_ENDPOINT` (→ `tempo-distributor.observability.svc.cluster.local:4317`),
  `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_PROPAGATORS=tracecontext,baggage`, `OTEL_SERVICE_NAME`,
  `OTEL_RESOURCE_ATTRIBUTES`.
- Downward API: `POD_NAME`, `POD_NAMESPACE`, `POD_IP`.
- Contract 13 (when `database.enabled`): `POSTGRES_HOST/PORT/DB/SCHEMA/USER`, `POSTGRES_PASSWORD`
  (from the Doppler-synced runtime Secret), and `POSTGRES_DSN` assembled at runtime via
  `$(POSTGRES_PASSWORD)` env interpolation so the secret never appears in the manifest. The
  migration Job builds `ATLAS_URL` the same way from `DDL_PASSWORD`.

Required per-service **values** (consumer must set): `service`, `image.repository`/`tag`,
`environment`, `nodeRole`, `database.schema`/`runtimeUser`/`ddlUser`,
`doppler.runtimeConfig`/`ddlConfig`. Secrets live in **Doppler** (project `cypherx-platform`):
`db/<svc>/runtime_password` (runtime config, e.g. `shared-core.<svc>`) and `db/<svc>/ddl_password`
(separate `ddlConfig`, Job only). The Doppler operator syncs each `DopplerSecret` CR to a K8s
Secret; the operator bootstrap token Secret defaults to `doppler-token-<release-namespace>`. No
`.env` here. For local `helm template` without the operator, set `doppler.enabled=false` and point
`externalRuntimeSecretName`/`externalDdlSecretName` at pre-existing Secrets.

## Contracts & cross-repo dependencies
Source of truth lives in `../contracts` (`logging/`, `health/`, `tracing/`, `tenant/`,
`migrations/`). This chart **enforces**:
- **Contract 6** — structured JSON logs: identity env + `logFormat` pinned `json`.
- **Contract 7** — health/metrics: `/livez` liveness (no downstreams), `/readyz` readiness,
  `http:8080` + `metrics:9090`, ServiceMonitor + scrape NetworkPolicy.
- **Contract 8** — W3C trace propagation to Tempo via OTLP gRPC.
- **Contract 13** — tenant/RLS: `POSTGRES_DSN` → transaction-mode PgBouncer
  `pgbouncer.data.svc.cluster.local:6432` (required for `SET LOCAL app.tenant_id`).
- **Contract 14** — schema migrations: Atlas Job as a `pre-install,pre-upgrade` hook
  (`atlas migrate apply --dir file://<migrationsDir> --url $(ATLAS_URL) --revisions-schema <schema>`).

**Consumed by:** every service repo (auth-service, llms-gateway, xAgent, tools, …) and the
**gitops** repo / ArgoCD. **DB role model it wires** (Phase 1 Component 16): runtime uses `*_user`
(least privilege), migration Job uses `*_ddl` (`CREATEROLE`); schemas/users are created by
Terraform, tables/indexes/RLS by Atlas. The chart itself produces/consumes **no Kafka topics**.

## Invariants & guards (do NOT break)
Backed by `values.schema.json` (`const`/`pattern`), chart READMEs, and Phase 1 Component 16:
- **Liveness is process-only** — `/livez` never checks downstreams; `/readyz` does. Probe paths are
  schema-locked to `^/(livez|readyz)$`. Never point liveness at `/readyz`.
- **Metrics port is `9090`** — schema `const: 9090`. The Istio `metrics-permissive`
  PeerAuthentication and the observability scrape NetworkPolicy assume it.
- **`logFormat` is `const: "json"`**; `otelPropagators` must match `tracecontext` (no b3/jaeger).
- **DSN → PgBouncer transaction mode** — never RDS-direct (breaks `SET LOCAL` tenant isolation).
- **Migration Job uses `*_ddl`, runtime uses `*_user`** — the DDL password is a *separate*
  `DopplerSecret` mounted only into the Job via a dedicated SA with
  `automountServiceAccountToken: false`. This isolation is the documented mitigation for
  cluster-wide `CREATEROLE` (Postgres has no schema-scoped CREATEROLE). Never reuse the runtime SA;
  never share a DDL user across services.
- **Migration hook `delete-policy` is `before-hook-creation`** on the Job (not `hook-failed`) so a
  failed Job is retained for triage — Helm aborts the upgrade and the previous Deployment keeps
  serving. (The migration SA additionally carries `hook-succeeded`.)
- **Selector labels are minimal** (`app.kubernetes.io/name` + `instance`) — never add
  version/environment to `selectorLabels` (a rolling deploy would orphan pods).
- **Security context is enforced**: `runAsNonRoot:true`, `readOnlyRootFilesystem:true`,
  `allowPrivilegeEscalation:false` are schema `const`; drop ALL caps; `/tmp` is an emptyDir.
- Schema is **strict** (`additionalProperties:false` everywhere) — a typo'd value key fails `helm lint`.
- Do NOT add service-specific business logic here — only platform-wide, contract-level defaults.

## Gotchas & current status
- **Service values key is `service_`** (trailing underscore), not `service` — `service` is the
  identity string (Contract 6 / `cypherx.ai/service` label); `service_` holds the K8s Service
  `type`/`annotations`. Easy to mis-set.
- **The base chart's `values.yaml` is not lint-clean alone** — `image.repository`, `database.schema`,
  `doppler.runtimeConfig`, etc. are intentionally empty. Render via `example-service`, not the base
  chart directly. `doppler.runtimeConfig` is `required` (template-side) when `doppler.enabled=true`.
- The schema declares a permissive top-level `global` (`additionalProperties:true`) so the strict
  base chart still validates when Helm injects `global` as a subchart dependency.
- **No `Chart.lock` / vendored `charts/*.tgz`** committed (gitignored) — consumers must run
  `helm dependency build` first.
- Migrations are expected baked into the service image at `migration.migrationsDir` (`/migrations`,
  Atlas `file://` source); supply a volume via `migration.extraVolumes` otherwise.
- The migration Job pins `sidecar.istio.io/inject: "false"` (a sidecar would block Job completion).
- Single-commit Phase 1 deliverable (same commit on `development` and `feature/base-implementation`);
  no tests beyond `helm lint`/`helm template` smoke via example-service, and no published OCI
  chart registry yet.
