# echo-service

A single-purpose FastAPI app used **only** by the CypherX Phase 1 infrastructure
smoke test (Phase 1, Component 21). It is deployed to a throwaway `smoketest`
namespace, exercised by `infra/scripts/infra-smoke-test.sh`, then torn down. It
is **not** a product service and must never be deployed outside the smoke gate.

## What it proves

The app deliberately touches every plumbing layer the gate asserts on:

| Capability | Contract / Component | Backs assertion |
|---|---|---|
| `GET /echo` returns request headers as JSON + emits ONE Contract 6 JSON log line | Contract 6 | 1, 2 |
| `traceparent` echoed on the response (populated by Istio at the edge) | Contract 8 | 1, 4 |
| `/livez`, `/readyz`, `/metrics` | Contract 7 | 6, 8 |
| `/metrics` exposed on `:9090` (Istio metrics-permissive port) | Component 7 | 6 |
| Produces ONE Contract 5 envelope to `cypherx.smoketest.event` on startup | Contract 5 | 5 |
| PgBouncer RLS round-trip `BEGIN; SET LOCAL app.tenant_id; SELECT 1; COMMIT` + `rls_probe=ok` log | Contract 13 | 7 |
| Valkey `PING` on startup; readiness fails if DB **or** Valkey is down | Contract 7 / Component 5 | (readiness) |
| Echoes `SMOKE_SECRET_LEN` (length only) of a Doppler-synced secret | Component 11/20 | 9 |

The Contract 5 envelope's `partition_key` is the **fake integration-test tenant**
`00000000-0000-0000-0000-0000000000ff` (`contracts/tenant/well-known.md`). That
UUID is CI-only and rejected in prod — correct for a throwaway smoke test.

> `cypherx.smoketest.event` is a normal `delete` topic, so `partition_key =
> tenant_id` is correct. The `agent_id` key override (Component 17) is **only**
> for compact `auth.agent.*` topics — do not apply it here.

## Endpoints

| Method | Path | Port | Purpose |
|---|---|---|---|
| GET | `/echo` | 8080 | request headers as JSON; one Contract 6 log line |
| GET | `/livez` | 8080 | liveness — never checks downstreams (Contract 7) |
| GET | `/readyz` | 8080 | readiness — 503 if Postgres or Valkey unhealthy |
| GET | `/metrics` | 9090 | Prometheus exposition (`http_requests_total`, ...) |

## Configuration (all from env; injected by the chart / DopplerSecret)

`SERVICE`, `VERSION`, `ENVIRONMENT`, `LOG_LEVEL`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
`POSTGRES_*` (via the transaction-mode PgBouncer pooler), `VALKEY_HOST/PORT/PASSWORD/TLS`,
`KAFKA_BROKERS` + `KAFKA_SASL_*`, `SMOKE_TENANT_ID`, `SMOKE_SECRET`.
**No secret is ever hardcoded** — passwords arrive from the Doppler-synced K8s
Secret (Component 11/20). See `echo_service/config.py`.

## Build & run locally

```bash
# Build the multi-stage, non-root image
docker build -t cypherx/echo-service:smoketest infra/smoketest/echo-service

# Run against local Redpanda/Postgres/Valkey (Component 17c dev stack)
docker run --rm -p 8080:8080 -p 9090:9090 \
  -e KAFKA_BROKERS=host.docker.internal:9092 \
  -e KAFKA_SECURITY_PROTOCOL=PLAINTEXT \
  -e POSTGRES_DSN='postgresql://echo:echo@host.docker.internal:6432/cypherx_platform' \
  -e VALKEY_HOST=host.docker.internal -e VALKEY_TLS=false \
  -e SMOKE_SECRET=hello \
  cypherx/echo-service:smoketest

curl -s localhost:8080/echo | jq        # headers + smoke_secret_len
curl -s localhost:8080/livez | jq       # liveness
curl -s localhost:8080/readyz | jq      # readiness (needs PG + Valkey up)
curl -s localhost:9090/metrics | head   # Prometheus exposition
```

## Layout

```
echo-service/
├── pyproject.toml            ← pinned deps, py>=3.12
├── Dockerfile                ← multi-stage, runs as uid/gid 10001 (matches chart)
├── .dockerignore
└── echo_service/
    ├── __main__.py           ← runs :8080 app + :9090 metrics in one process
    ├── app.py                ← /echo /livez /readyz /metrics + startup lifespan
    ├── config.py             ← env-only settings (no hardcoded secrets)
    ├── deps.py               ← PgBouncer RLS probe, Valkey PING, Kafka produce
    ├── envelope.py           ← Contract 5 envelope builder
    ├── logging_setup.py      ← Contract 6 JSON log lines
    └── tracing.py            ← Contract 8 W3C trace context + OTLP gRPC -> Tempo
```

Deployed via the `charts/cypherx-service` base chart driven by
`infra/smoketest/values.yaml`. See `infra/smoketest/README.md` to run the gate.
