#!/usr/bin/env bash
# =====================================================================================================================
# kafka-bootstrap-topics.sh — ONE-SHOT FALLBACK topic bootstrap (Component 17).
#
# The authoritative, drift-detected source of truth is the Terraform module modules/kafka-topics (Mongey/kafka
# provider) wired via environments/<env>/kafka-topics/terragrunt.hcl. PREFER THAT.
#
# This script exists ONLY as an emergency/break-glass fallback (Component 17: "Script (one-shot fallback):
# infra/scripts/kafka-bootstrap-topics.sh"). It mirrors the EXACT same topic set, partitions, replication,
# cleanup.policy, retention, and common config. It does NOT reconcile drift — run Terraform once the cluster is
# back to a normal operating state.
#
# Supports two CLIs (auto-detected, override with TOOL=rpk|kafka-topics):
#   - rpk            (Redpanda CLI; also speaks to MSK)
#   - kafka-topics   (Apache Kafka admin CLI: kafka-topics.sh / kafka-topics)
#
# Usage:
#   BOOTSTRAP_SERVERS="b-1...:9096,b-2...:9096,b-3...:9096" \
#   SASL_USERNAME="..." SASL_PASSWORD="..." \
#   ./kafka-bootstrap-topics.sh
#
# SASL creds come from Doppler / env — NEVER hardcode them here.
# =====================================================================================================================
set -euo pipefail

# ---------------------------------------------------------------------------------------------------------------------
# Config (env-driven). MSK SCRAM-SHA-512 listener is port 9096.
# ---------------------------------------------------------------------------------------------------------------------
BOOTSTRAP_SERVERS="${BOOTSTRAP_SERVERS:?set BOOTSTRAP_SERVERS to the MSK SASL_SSL broker list}"
REPLICATION_FACTOR="${REPLICATION_FACTOR:-3}"
SASL_USERNAME="${SASL_USERNAME:-}"
SASL_PASSWORD="${SASL_PASSWORD:-}"
SASL_MECHANISM="${SASL_MECHANISM:-SCRAM-SHA-512}"
SECURITY_PROTOCOL="${SECURITY_PROTOCOL:-SASL_SSL}"
TOOL="${TOOL:-auto}"

# Retention (ms). -1 == infinite (compact topics).
MS_PER_DAY=86400000
RET_INFINITE=-1
RET_30D=$((30 * MS_PER_DAY))
RET_90D=$((90 * MS_PER_DAY))
RET_365D=$((365 * MS_PER_DAY))

# Common config applied to EVERY topic (Component 17).
COMMON_CONFIG=(
  "min.insync.replicas=2"
  "unclean.leader.election.enable=false"
  "compression.type=lz4"
)

# ---------------------------------------------------------------------------------------------------------------------
# Topic table — EXACT Component 17 spec. Format: "name|partitions|cleanup.policy|retention.ms"
# Compact topics (auth.agent.*) get NO DLQ; all others get a paired .dlq (same partitions, delete, 30d).
# ---------------------------------------------------------------------------------------------------------------------
CORE_TOPICS=(
  "cypherx.auth.agent.registered|6|compact|${RET_INFINITE}"
  "cypherx.auth.agent.deactivated|6|compact|${RET_INFINITE}"
  "cypherx.llms.request.completed|12|delete|${RET_90D}"
  "cypherx.llms.budget.alert|3|delete|${RET_30D}"
  "cypherx.guardrails.violation.detected|12|delete|${RET_90D}"
  "cypherx.agent.task.submitted|24|delete|${RET_30D}"
  "cypherx.agent.task.completed|24|delete|${RET_30D}"
  "cypherx.agent.task.failed|24|delete|${RET_30D}"
  "cypherx.platform.audit.event|12|delete|${RET_365D}"
  "cypherx.billing.usage.recorded|6|delete|${RET_365D}"
)
DLQ_RETENTION_MS=$RET_30D

# ---------------------------------------------------------------------------------------------------------------------
# Tool detection.
# ---------------------------------------------------------------------------------------------------------------------
detect_tool() {
  if [[ "$TOOL" != "auto" ]]; then echo "$TOOL"; return; fi
  if command -v rpk >/dev/null 2>&1; then echo "rpk"; return; fi
  if command -v kafka-topics.sh >/dev/null 2>&1; then echo "kafka-topics.sh"; return; fi
  if command -v kafka-topics >/dev/null 2>&1; then echo "kafka-topics"; return; fi
  echo "ERROR: no rpk or kafka-topics CLI found on PATH" >&2
  exit 1
}
TOOL_BIN="$(detect_tool)"
echo "==> Using CLI: ${TOOL_BIN}"
echo "==> Bootstrap: ${BOOTSTRAP_SERVERS}  RF=${REPLICATION_FACTOR}"

# ---------------------------------------------------------------------------------------------------------------------
# rpk: write a transient profile for SASL_SSL + SCRAM.
# ---------------------------------------------------------------------------------------------------------------------
rpk_common_args=()
if [[ "$TOOL_BIN" == "rpk" ]]; then
  rpk_common_args=(--brokers "$BOOTSTRAP_SERVERS")
  if [[ -n "$SASL_USERNAME" ]]; then
    rpk_common_args+=(--user "$SASL_USERNAME" --password "$SASL_PASSWORD" --sasl-mechanism "$SASL_MECHANISM" --tls-enabled)
  fi
fi

# kafka-topics: build a client config file for SASL_SSL (creds from env, not committed).
KAFKA_CLIENT_CONFIG=""
if [[ "$TOOL_BIN" == "kafka-topics.sh" || "$TOOL_BIN" == "kafka-topics" ]]; then
  KAFKA_CLIENT_CONFIG="$(mktemp)"
  trap 'rm -f "$KAFKA_CLIENT_CONFIG"' EXIT
  {
    echo "security.protocol=${SECURITY_PROTOCOL}"
    echo "sasl.mechanism=${SASL_MECHANISM}"
    if [[ -n "$SASL_USERNAME" ]]; then
      echo "sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username=\"${SASL_USERNAME}\" password=\"${SASL_PASSWORD}\";"
    fi
  } > "$KAFKA_CLIENT_CONFIG"
fi

# ---------------------------------------------------------------------------------------------------------------------
# create_topic <name> <partitions> <cleanup.policy> <retention.ms>
# Idempotent: "already exists" is treated as success.
# ---------------------------------------------------------------------------------------------------------------------
create_topic() {
  local name="$1" partitions="$2" cleanup="$3" retention="$4"
  echo "  -> ${name} (partitions=${partitions}, cleanup=${cleanup}, retention.ms=${retention})"

  if [[ "$TOOL_BIN" == "rpk" ]]; then
    local cfg_args=()
    for c in "${COMMON_CONFIG[@]}"; do cfg_args+=(--topic-config "$c"); done
    cfg_args+=(--topic-config "cleanup.policy=${cleanup}" --topic-config "retention.ms=${retention}")
    if ! rpk topic create "$name" -p "$partitions" -r "$REPLICATION_FACTOR" "${cfg_args[@]}" "${rpk_common_args[@]}" 2>&1 \
        | tee /tmp/rpk_out | grep -qiE 'OK|already exists|TOPIC_ALREADY_EXISTS'; then
      grep -qiE 'already exists' /tmp/rpk_out || { echo "FAILED creating ${name}" >&2; cat /tmp/rpk_out >&2; exit 1; }
    fi
  else
    local cfg_args=()
    for c in "${COMMON_CONFIG[@]}"; do cfg_args+=(--config "$c"); done
    cfg_args+=(--config "cleanup.policy=${cleanup}" --config "retention.ms=${retention}")
    if ! "$TOOL_BIN" --bootstrap-server "$BOOTSTRAP_SERVERS" \
        --command-config "$KAFKA_CLIENT_CONFIG" \
        --create --if-not-exists \
        --topic "$name" \
        --partitions "$partitions" \
        --replication-factor "$REPLICATION_FACTOR" \
        "${cfg_args[@]}"; then
      echo "FAILED creating ${name}" >&2
      exit 1
    fi
  fi
}

# ---------------------------------------------------------------------------------------------------------------------
# Create core topics + DLQs.
# ---------------------------------------------------------------------------------------------------------------------
echo "==> Creating core topics + DLQs..."
for row in "${CORE_TOPICS[@]}"; do
  IFS='|' read -r name partitions cleanup retention <<<"$row"
  create_topic "$name" "$partitions" "$cleanup" "$retention"

  # Paired DLQ for NON-compact topics only (Component 17). Same partitions, delete, 30d retention.
  if [[ "$cleanup" != "compact" ]]; then
    create_topic "${name}.dlq" "$partitions" "delete" "$DLQ_RETENTION_MS"
  fi
done

echo "==> Done."
echo
echo "REMINDER (Component 17): producers of the compact cypherx.auth.agent.* topics MUST set the Kafka message key"
echo "to agent_id (NOT tenant_id). A tenant_id-keyed compact topic collapses to one record per tenant and loses"
echo "every prior agent state. This script only creates topics — the key is set producer-side."
echo
echo "This was the one-shot fallback. Run 'terragrunt apply' on environments/<env>/kafka-topics to restore the"
echo "drift-detected, declarative source of truth."
