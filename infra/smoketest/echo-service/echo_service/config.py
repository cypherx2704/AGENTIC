"""Runtime configuration for echo-service.

All values come from the environment. The cypherx-service base Helm chart
(charts/cypherx-service) injects SERVICE / VERSION / ENVIRONMENT / LOG_* /
OTEL_* / POSTGRES_* automatically (see its deployment.yaml). The smoke-test
specific knobs (Kafka, Valkey, the Doppler-synced SMOKE_SECRET) are supplied
by smoketest/values.yaml extraEnv + the test DopplerSecret.

No secret value is ever hardcoded here — passwords arrive via env from the
Doppler-synced K8s Secret (Component 11/20). This module only reads them.
"""

from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """Process-wide settings, read once at import time."""

    # ---- Contract 6 identity fields (chart-injected) ----
    service: str = os.getenv("SERVICE", "echo")
    version: str = os.getenv("VERSION", "0.1.0")
    environment: str = os.getenv("ENVIRONMENT", "dev")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # ---- HTTP / metrics ports (Contract 7; chart convention 8080 / 9090) ----
    http_port: int = int(os.getenv("HTTP_PORT", os.getenv("PORT", "8080")))
    metrics_port: int = int(os.getenv("METRICS_PORT", "9090"))

    # ---- Contract 8 trace export (chart-injected) ----
    otel_endpoint: str = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://tempo-distributor.observability.svc.cluster.local:4317",
    )
    otel_enabled: bool = _bool("OTEL_ENABLED", True)

    # ---- Contract 13: Postgres via the transaction-mode PgBouncer pooler ----
    # Assembled DSN is provided by the chart as POSTGRES_DSN; we also accept the
    # discrete parts so the app works under `helm template`/local runs.
    pg_dsn: str = os.getenv("POSTGRES_DSN", "")
    pg_host: str = os.getenv("POSTGRES_HOST", "pgbouncer.data.svc.cluster.local")
    pg_port: int = int(os.getenv("POSTGRES_PORT", "6432"))
    pg_db: str = os.getenv("POSTGRES_DB", "cypherx_platform")
    pg_user: str = os.getenv("POSTGRES_USER", "")
    pg_password: str = os.getenv("POSTGRES_PASSWORD", "")
    pg_schema: str = os.getenv("POSTGRES_SCHEMA", "public")

    # ---- Valkey (ElastiCache; Component 5: TLS + AUTH token) ----
    valkey_host: str = os.getenv("VALKEY_HOST", "valkey.data.svc.cluster.local")
    valkey_port: int = int(os.getenv("VALKEY_PORT", "6379"))
    valkey_password: str = os.getenv("VALKEY_PASSWORD", "")
    valkey_tls: bool = _bool("VALKEY_TLS", True)

    # ---- Kafka (MSK; Component 5: TLS + SASL/SCRAM-SHA-512) ----
    kafka_brokers: str = os.getenv("KAFKA_BROKERS", "")
    kafka_topic: str = os.getenv("KAFKA_SMOKETEST_TOPIC", "cypherx.smoketest.event")
    kafka_security_protocol: str = os.getenv("KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
    kafka_sasl_mechanism: str = os.getenv("KAFKA_SASL_MECHANISM", "SCRAM-SHA-512")
    kafka_sasl_username: str = os.getenv("KAFKA_SASL_USERNAME", "")
    kafka_sasl_password: str = os.getenv("KAFKA_SASL_PASSWORD", "")

    # ---- Contract 5 / Contract 13: the fake tenant the envelope is keyed on ----
    # Well-known integration-test tenant (contracts/tenant/well-known.md). CI-only;
    # this UUID is rejected in prod, which is exactly what we want for a smoke test.
    fake_tenant_id: str = os.getenv(
        "SMOKE_TENANT_ID", "00000000-0000-0000-0000-0000000000ff"
    )

    # ---- Assertion 9: a Doppler-synced secret echoed back as its length only ----
    # We deliberately echo only the LENGTH, never the value, so /echo can be
    # asserted on without leaking the secret into logs or the response body.
    smoke_secret: str = os.getenv("SMOKE_SECRET", "")


settings = Settings()
