#!/bin/sh
# =====================================================================================================================
# infra/compose/topics-init.sh — one-shot Kafka (Redpanda) topic bootstrap. Idempotent.
#
# Creates EVERY topic the CypherX services publish/consume so a cold cluster has them ahead of the producers/
# consumers. Redpanda auto-creates topics on first produce, but we pre-create with explicit partitions/retention so
# the names + config are deterministic and a consumer never races a missing topic. Re-running is safe: a topic that
# already exists is reported and skipped (TOPIC_ALREADY_EXISTS is swallowed; any other error fails the job).
#
# Mounted read-only by docker-compose.yml and run by the 'topics-init' service (redpanda image -> rpk available).
# Tunables come from the environment (fed from .env): KAFKA_TOPIC_PARTITIONS, KAFKA_TOPIC_RETENTION_MS.
#
# Topics gathered from the services' outbox/consumer code:
#   auth        cypherx.auth.{token.revoked,policy.changed,config.updated,audit.appended,agent.deactivated,agent.updated}
#               cypherx.tenant.{created,suspended,resumed,plan_changed,pending_deletion,deleted}  (Contract-13 backbone)
#   llms        cypherx.llms.{request.completed,usage.recorded}
#   guardrails  cypherx.guardrails.{violation.detected,usage.recorded,policy.changed}
#   xagent      cypherx.agent.{task.completed,task.failed,tools.invocation.metered}
#   rag         cypherx.rag.{ingestion.requested,ingestion.completed,ingestion.failed,usage.recorded}
#   memory      cypherx.memory.{stored,deleted,gdpr.wiped}
# =====================================================================================================================
set -e

BROKER="${KAFKA_INIT_BROKER:-redpanda:29092}"
PARTITIONS="${KAFKA_TOPIC_PARTITIONS:-1}"
RETENTION="${KAFKA_TOPIC_RETENTION_MS:-604800000}"   # 7d default

create() {
  out=$(rpk topic create "$1" -X brokers="$BROKER" -p "$PARTITIONS" -c retention.ms="$RETENTION" 2>&1) \
    && echo "  created $1" \
    || (echo "$out" | grep -qi 'already exists\|TOPIC_ALREADY_EXISTS' && echo "  exists  $1") \
    || (echo "$out" >&2; exit 1)
}

echo "topics-init: creating CypherX Kafka topics on $BROKER (p=$PARTITIONS, retention.ms=$RETENTION)"

# auth (WP02/WP03/WP04)
create cypherx.auth.token.revoked
create cypherx.auth.policy.changed
create cypherx.auth.config.updated
create cypherx.auth.audit.appended
create cypherx.auth.agent.deactivated
create cypherx.auth.agent.updated
# tenant lifecycle backbone (Contract 13; Auth is the producer)
create cypherx.tenant.created
create cypherx.tenant.suspended
create cypherx.tenant.resumed
create cypherx.tenant.plan_changed
create cypherx.tenant.pending_deletion
create cypherx.tenant.deleted
# llms-gateway
create cypherx.llms.request.completed
create cypherx.llms.usage.recorded
# guardrails-service
create cypherx.guardrails.violation.detected
create cypherx.guardrails.usage.recorded
create cypherx.guardrails.policy.changed
# xagent (agent-runtime)
create cypherx.agent.task.completed
create cypherx.agent.task.failed
create cypherx.agent.tools.invocation.metered
# rag-service
create cypherx.rag.ingestion.requested
create cypherx.rag.ingestion.completed
create cypherx.rag.ingestion.failed
create cypherx.rag.usage.recorded
# memory-service
create cypherx.memory.stored
create cypherx.memory.deleted
create cypherx.memory.gdpr.wiped

echo "topics-init: done"
