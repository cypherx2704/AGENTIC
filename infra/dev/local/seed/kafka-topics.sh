#!/usr/bin/env bash
# =====================================================================================================================
# dev/local/seed/kafka-topics.sh — create the CypherX dev core Kafka topics on the local Redpanda broker.
#
# Mirrors the Component 17 core topic set + DLQ pairings, SIMPLIFIED for a laptop:
#   - partitions   = 1   (cloud uses 3/6/12/24 — irrelevant for single-broker local ordering)
#   - replication  = 1   (cloud uses 3 — a single Redpanda node cannot do 3)
#   - cleanup.policy and retention.ms are kept faithful so consumers behave like prod (compaction vs delete).
#
# Run automatically by the Tiltfile's `redpanda-topics` resource once Redpanda is healthy, or by hand:
#   docker compose -f dev/local/docker-compose.yml exec -T redpanda bash < dev/local/seed/kafka-topics.sh
# or, if you have rpk on the host:
#   RPK_BROKERS=localhost:9092 bash dev/local/seed/kafka-topics.sh
#
# Idempotent: `rpk topic create` is a no-op (exit 0 after a warning) if the topic already exists.
#
# COMPACT-TOPIC KEY RULE (Component 17 / Contract 5 — do NOT remove): producers to the compact auth.agent.* topics
# MUST set the Kafka message key to agent_id (NOT tenant_id). A tenant_id-keyed compact topic collapses to one record
# per tenant and loses every prior agent state. This script only creates the topics; the rule is enforced by producers.
# =====================================================================================================================
set -euo pipefail

# rpk reaches the broker over the EXTERNAL (host) or INTERNAL listener depending on where this runs.
BROKERS="${RPK_BROKERS:-localhost:9092}"
RPK=(rpk topic create -X brokers="${BROKERS}")

echo "Creating CypherX dev topics on Redpanda (${BROKERS}) ..."

# --- helpers -------------------------------------------------------------------------------------------------------
# create_delete <topic> <retention_ms>   → a normal delete-retention topic + its paired <topic>.dlq (30-day retention)
# create_compact <topic>                  → a compacted topic (infinite retention); NO DLQ (Component 17 rule)
DAY_MS=$((24 * 60 * 60 * 1000))

create_delete() {
  local topic="$1" retention_ms="$2"
  "${RPK[@]}" "${topic}" \
    --partitions 1 --replicas 1 \
    --topic-config cleanup.policy=delete \
    --topic-config retention.ms="${retention_ms}" \
    --topic-config compression.type=lz4 || true

  # Paired DLQ — same (simplified) shape, fixed 30-day retention per Contract 5 / Component 17.
  "${RPK[@]}" "${topic}.dlq" \
    --partitions 1 --replicas 1 \
    --topic-config cleanup.policy=delete \
    --topic-config retention.ms="$((30 * DAY_MS))" \
    --topic-config compression.type=lz4 || true
}

create_compact() {
  local topic="$1"
  # cleanup.policy=compact + retention.ms=-1 (infinite). Compact topics get NO DLQ (re-read from latest state).
  "${RPK[@]}" "${topic}" \
    --partitions 1 --replicas 1 \
    --topic-config cleanup.policy=compact \
    --topic-config retention.ms=-1 \
    --topic-config compression.type=lz4 || true
}

# --- compact topics (auth.agent.*) — infinite retention, agent_id-keyed by producers, NO DLQ -----------------------
create_compact "cypherx.auth.agent.registered"
create_compact "cypherx.auth.agent.deactivated"

# --- delete topics (+ DLQ) — retention per the Component 17 table -------------------------------------------------
create_delete "cypherx.llms.request.completed"        "$((90  * DAY_MS))"   # 90 days
create_delete "cypherx.llms.budget.alert"             "$((30  * DAY_MS))"   # 30 days
create_delete "cypherx.guardrails.violation.detected" "$((90  * DAY_MS))"   # 90 days
create_delete "cypherx.agent.task.submitted"          "$((30  * DAY_MS))"   # 30 days
create_delete "cypherx.agent.task.completed"          "$((30  * DAY_MS))"   # 30 days
create_delete "cypherx.agent.task.failed"             "$((30  * DAY_MS))"   # 30 days
create_delete "cypherx.platform.audit.event"          "$((365 * DAY_MS))"   # 365 days
create_delete "cypherx.billing.usage.recorded"        "$((365 * DAY_MS))"   # 365 days

# --- smoke-test topic (Component 21) — used by the infra smoke test echo-service ---------------------------------
create_delete "cypherx.smoketest.event"               "$((1   * DAY_MS))"   # 1 day, local only

echo ""
echo "Done. Current topics:"
rpk topic list -X brokers="${BROKERS}"
