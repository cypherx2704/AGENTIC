# example-service

A **values-only** consumer of the `cypherx-service` base chart. It carries no
templates of its own — it declares `cypherx-service` as a dependency and supplies
the per-service overrides a real service repo would set. Use it to:

- `helm lint` / `helm template` the base chart standalone (CI smoke for the chart),
- as the copy-me reference for a new service repo.

It mirrors the Phase 1 echo-service: owns a Postgres schema (`example`), runs
Atlas migrations, lives in an Istio-injected namespace.

## Render it

```bash
# from charts/example-service
helm dependency build            # pulls cypherx-service from file://../cypherx-service
helm lint . -f values.yaml
helm template ex . -f values.yaml

# prod overlay
helm template ex . -f values.yaml -f values-prod.yaml
```

## What a real service changes

Copy `values.yaml`, then change (at minimum):

- `cypherx-service.service` — your service name
- `cypherx-service.image.repository` / `.tag`
- `cypherx-service.database.schema` / `.runtimeUser` / `.ddlUser`
- `cypherx-service.doppler.runtimeConfig` / `.ddlConfig`
- `cypherx-service.nodeRole` — `core` (SharedCore), `agent` (xAgent), `tools`

Everything else (probes, OTEL endpoint, PgBouncer host, log format, security
context, NetworkPolicy) comes from the base chart defaults and should not be
overridden without a platform review.
