"""Downstream dependency probes for echo-service.

Three downstreams are exercised, matching Component 21:
  * Postgres via the transaction-mode PgBouncer pooler — the RLS probe
    (`BEGIN; SET LOCAL app.tenant_id; SELECT 1; COMMIT`) backing assertion 7.
  * Valkey PING — readiness (Component 5 cache reachability).
  * Kafka — produce ONE Contract 5 envelope on startup (assertion 5).

Readiness (Contract 7) fails if Postgres or Valkey is unhealthy. Liveness never
touches any of these (Contract 7: liveness MUST NOT depend on downstreams).
"""

from __future__ import annotations

import json
import ssl
import uuid

import asyncpg
import redis.asyncio as aioredis
from aiokafka import AIOKafkaProducer
from aiokafka.helpers import create_ssl_context

from . import __version__
from .config import settings
from .envelope import build_envelope
from .logging_setup import log


async def pg_rls_probe() -> bool:
    """Run the Contract 13 RLS round-trip through PgBouncer (transaction mode).

    Emits the `rls_probe=ok` log line (assertion 7) on success. The whole probe
    runs inside ONE explicit transaction so `SET LOCAL app.tenant_id` is scoped
    to the transaction — exactly the pattern every tenant-scoped service must use
    and the thing PgBouncer `pool_mode=transaction` makes safe.
    """
    dsn = settings.pg_dsn or (
        f"postgresql://{settings.pg_user}:{settings.pg_password}"
        f"@{settings.pg_host}:{settings.pg_port}/{settings.pg_db}"
    )
    conn = None
    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=5)
        # Explicit transaction: BEGIN ... SET LOCAL ... SELECT 1 ... COMMIT.
        tx = conn.transaction()
        await tx.start()
        # SET LOCAL cannot be parameterised; the value is a fixed well-known UUID,
        # not user input, so quoting it inline is safe here.
        await conn.execute(f"SET LOCAL app.tenant_id = '{settings.fake_tenant_id}'")
        result = await conn.fetchval("SELECT 1")
        await tx.commit()
        ok = result == 1
        if ok:
            # The exact log line assertion 7 greps for.
            log(
                "INFO",
                "PgBouncer RLS probe succeeded",
                tenant_id=settings.fake_tenant_id,
                rls_probe="ok",
                pgbouncer_host=settings.pg_host,
                pool_mode="transaction",
            )
        return ok
    except Exception as exc:  # noqa: BLE001 — readiness must catch everything
        log("ERROR", "PgBouncer RLS probe failed", rls_probe="failed", error=str(exc))
        return False
    finally:
        if conn is not None:
            await conn.close()


def _valkey_client() -> aioredis.Redis:
    kwargs: dict = {
        "host": settings.valkey_host,
        "port": settings.valkey_port,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    }
    if settings.valkey_password:
        kwargs["password"] = settings.valkey_password
    if settings.valkey_tls:
        # ElastiCache Valkey terminates TLS (Component 5: TLS enabled).
        kwargs["ssl"] = True
        kwargs["ssl_cert_reqs"] = "required"
    return aioredis.Redis(**kwargs)


async def valkey_ping() -> bool:
    """PING Valkey. Returns True on PONG (Component 5 cache reachability)."""
    client = _valkey_client()
    try:
        pong = await client.ping()
        ok = bool(pong)
        if ok:
            log("INFO", "Valkey PING succeeded", valkey_host=settings.valkey_host)
        return ok
    except Exception as exc:  # noqa: BLE001
        log("ERROR", "Valkey PING failed", error=str(exc))
        return False
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


def _kafka_ssl_context() -> ssl.SSLContext | None:
    if settings.kafka_security_protocol in ("SSL", "SASL_SSL"):
        return create_ssl_context()
    return None


async def produce_startup_event(trace_id: str) -> bool:
    """Produce exactly ONE Contract 5 envelope to cypherx.smoketest.event.

    Backs assertion 5. The Kafka message KEY is set to partition_key (the fake
    tenant_id) so per-tenant ordering holds — this topic is a normal `delete`
    topic, not a compact one, so tenant_id keying is correct (the agent_id
    override in Component 17 applies only to compact auth.agent.* topics).
    """
    if not settings.kafka_brokers:
        log("WARN", "KAFKA_BROKERS unset — skipping startup event", topic=settings.kafka_topic)
        return False

    envelope = build_envelope(
        event_type="cypherx.smoketest.event",
        trace_id=trace_id,
        tenant_id=settings.fake_tenant_id,
        producer_service=settings.service,
        producer_version=__version__,
        payload={
            "smoke_run_id": str(uuid.uuid4()),
            "note": "infra smoke-test startup event (Component 21)",
        },
    )

    producer_kwargs: dict = {
        "bootstrap_servers": settings.kafka_brokers,
        "client_id": f"{settings.service}-smoketest",
        "acks": "all",  # min.insync.replicas=2 on MSK (Component 17)
        "enable_idempotence": True,
    }
    protocol = settings.kafka_security_protocol
    if protocol != "PLAINTEXT":
        producer_kwargs["security_protocol"] = protocol
        ssl_ctx = _kafka_ssl_context()
        if ssl_ctx is not None:
            producer_kwargs["ssl_context"] = ssl_ctx
    if protocol.startswith("SASL"):
        producer_kwargs["sasl_mechanism"] = settings.kafka_sasl_mechanism
        producer_kwargs["sasl_plain_username"] = settings.kafka_sasl_username
        producer_kwargs["sasl_plain_password"] = settings.kafka_sasl_password

    producer = AIOKafkaProducer(**producer_kwargs)
    try:
        await producer.start()
        await producer.send_and_wait(
            settings.kafka_topic,
            value=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
            # Kafka message key == envelope.partition_key (Contract 5).
            key=envelope["partition_key"].encode("utf-8"),
        )
        log(
            "INFO",
            "Produced Contract 5 smoke-test event",
            trace_id=trace_id,
            tenant_id=settings.fake_tenant_id,
            topic=settings.kafka_topic,
            event_id=envelope["event_id"],
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log("ERROR", "Failed to produce smoke-test event", topic=settings.kafka_topic, error=str(exc))
        return False
    finally:
        try:
            await producer.stop()
        except Exception:  # noqa: BLE001
            pass
