# Phase 1 Infrastructure Smoke Test (Component 21)

> **This is the gate that must pass before Phase 2 kickoff.** Phase 1 cannot be
> marked complete until this smoke test passes **two consecutive runs** against a
> freshly deployed dev environment (Phase 1 Component 21; Phase 0 Contract 15 is
> the service-level analogue run later).

The Infrastructure Health Checklist proves components are individually up. This
gate proves the *plumbing* works end-to-end: a log line written by a pod reaches
Loki, a trace id propagates through the Istio mesh, a Contract 5 Kafka event
round-trips, the PgBouncer RLS path works, Prometheus scrapes through the
metrics-permissive mTLS exception, Karpenter scales, and Doppler secrets sync —
then everything tears down with no orphans.

## What gets deployed

A throwaway **echo-service** (`smoketest/echo-service/`) in a throwaway
`smoketest` namespace, deployed via the `charts/cypherx-service` base chart
driven by `smoketest/values.yaml`. It is deleted at the end of every run.

```
smoketest/
├── echo-service/          ← the FastAPI app + Dockerfile (built & pushed to ECR)
├── Chart.yaml             ← thin wrapper: depends on charts/cypherx-service
├── values.yaml            ← drives the base chart (service=echo, ns wiring)
├── k8s/                   ← namespace, Kong route, DopplerSecret, raw fallbacks
└── README.md              ← (this file)

scripts/infra-smoke-test.sh  ← automates the 10 assertions; the gate runner
```

## The 10 assertions (Component 21)

| # | Assertion | How the script checks it |
|---|---|---|
| 1 | ALB → Kong → echo `GET /echo` → 200 with populated `traceparent` | curl through Kong; assert 200 + valid W3C `traceparent` |
| 2 | echo log line in Loki within 10s | `logcli`/Loki API `{service="echo"}` returns ≥ 1 |
| 3 | Loki labels low-cardinality only | labels exclude `tenant_id,agent_id,request_id,trace_id,span_id` |
| 4 | trace id in Tempo within 10s, spans cross Kong→echo | Tempo `/api/traces/<id>` has an echo span |
| 5 | `cypherx.smoketest.event` has 1 valid Contract 5 envelope | consume 1 msg; `jq` validates envelope + `partition_key==tenant_id` |
| 6 | echo `/metrics` scraped by Prometheus | `up{job="echo"} == 1` |
| 7 | PgBouncer accepted the RLS round-trip | echo log line shows `rls_probe=ok` |
| 8 | scale 1→3, Karpenter provisions if needed, all 3 Ready | `kubectl scale` + rollout status |
| 9 | Doppler operator synced a test secret | `/echo` reports `SMOKE_SECRET_LEN > 0` |
| 10 | clean teardown, no orphans | ns gone; no leaked topic / ALB target group / IAM role |

The script exits non-zero on the first failed assertion and requires
`--runs 2` consecutive green runs (the default) to pass the gate.

## Prerequisites

A freshly deployed **dev** environment with these Phase 1 pieces live:

- EKS reachable via `kubectl` (private endpoint via VPN, or in-VPC runner).
- Istio (STRICT mTLS + metrics-permissive on :9090, OTLP→Tempo), Kong + ALB,
  PgBouncer (transaction mode), Valkey, MSK, Prometheus + Loki + Promtail +
  Tempo, Doppler operator, metrics-server, **Karpenter** (`agent` NodePool).
- Doppler config `smoketest.echo` populated with `SMOKE_SECRET`,
  `POSTGRES_PASSWORD`, `VALKEY_PASSWORD`, `KAFKA_SASL_PASSWORD`, and the per-ns
  `doppler-token-secret` provisioned by Terraform (Component 11).
- `cypherx/echo-service` image built from `smoketest/echo-service/` and pushed
  to ECR (CI, Component 18 tagging convention — never `:latest`).

Tools on the runner: `kubectl`, `jq` (required); `helm`, `logcli`, `curl`,
`kcat`/`kafka-console-consumer`, `aws` (used when present).

## Build & push the image

```bash
docker build -t "$ECR/cypherx/echo-service:$GIT_SHA7" smoketest/echo-service
docker push "$ECR/cypherx/echo-service:$GIT_SHA7"
```

## Run the gate

```bash
export KAFKA_BROKERS="b-1.cypherx-dev.xxxx.kafka.us-east-1.amazonaws.com:9096,..."

scripts/infra-smoke-test.sh \
  --env dev \
  --runs 2 \
  --image "$ECR/cypherx/echo-service" \
  --tag "$GIT_SHA7"
```

Useful flags:

- `--no-helm` — deploy via the raw `k8s/` manifests instead of the base chart.
- `--keep` — skip teardown (leave `smoketest` ns up for debugging). Still exits
  non-zero on failure.
- `--skip-deploy` — re-run assertions against an already-deployed `smoketest`.
- `--namespace <ns>` — override the throwaway namespace name.

A green run ends with:

```
[smoke] ALL 2/2 runs green — Component 21 smoke gate PASSED
```

Record the result in the env's infra changelog; only then is Phase 1 complete
and Phase 2 may begin.

## Guardrails honoured (do NOT "fix" these)

- **ALB→Kong is plaintext** inside the VPC (`sg-kong` only accepts `sg-alb`);
  Kong→echo is mTLS via Istio. The Kong route here declares only the Kong side.
- **`partition_key = tenant_id`** for `cypherx.smoketest.event` — it is a normal
  `delete` topic, NOT a compact `auth.agent.*` topic, so the `agent_id` key
  override does not apply.
- **Metrics on :9090** so the Istio metrics-permissive PeerAuthentication lets
  the no-sidecar observability scraper reach it; STRICT mTLS stays everywhere
  else.
- The fake tenant is the **well-known integration-test tenant**
  `00000000-0000-0000-0000-0000000000ff` (CI-only; rejected in prod).
