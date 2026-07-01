package ai.cypherx.auth.kafka

import org.slf4j.LoggerFactory
import org.springframework.beans.factory.ObjectProvider
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Component
import java.time.Instant
import java.util.UUID

/**
 * Direct best-effort publisher for the ADVISORY auth events (Contract 5 envelopes built by
 * [EventEnvelopeFactory]).
 *
 * Scope (Phase 2 Amendment Log 2026-06 — event-fidelity split):
 *  - ADVISORY topics published here, best-effort: `cypherx.auth.agent.registered` and
 *    `cypherx.auth.agent.updated`. They are not provisioning-critical; consumers self-heal via
 *    TTL'd caches, so log-and-drop on broker failure is acceptable.
 *  - DURABLE topics (`cypherx.tenant.*`, `token.revoked`, `policy.changed`, `config.updated`)
 *    do NOT go through this class — they are written to `auth.outbox` in the same transaction
 *    as their state change via [OutboxEventWriter] and published by [OutboxRelay].
 *
 * Topic / key rules honoured here:
 *  - Topic name == `event_type` (Contract 5 §1: topic mirrors the fully-qualified event type).
 *  - The compact `cypherx.auth.agent.*` topics MUST be keyed by `agent_id` (topics.md §4.1) —
 *    otherwise log compaction collapses every agent in a tenant to one record. BOTH the Kafka
 *    message key AND the envelope `partition_key` are `agent_id`.
 *
 * Resilience: Kafka may be unreachable on a local boot with no broker. Sends are best-effort —
 * publish failures are logged at WARN and swallowed so request handling never fails because the
 * event bus is down (auth correctness does not depend on advisory delivery; durable state already
 * lives in PostgreSQL). If no [KafkaTemplate] bean is present at all, the publisher degrades to a
 * pure log fallback.
 */
@Component
class AuthEventPublisher(
    kafkaTemplateProvider: ObjectProvider<KafkaTemplate<String, String>>,
    private val envelopes: EventEnvelopeFactory,
) {

    /** Resolved once; null when Kafka auto-config produced no template (degrade to log fallback). */
    private val kafka: KafkaTemplate<String, String>? = kafkaTemplateProvider.ifAvailable

    /**
     * cypherx.auth.agent.registered — compact topic, keyed by agent_id (topics.md §4.1).
     * Payload matches contracts/kafka/events/auth.agent.registered.schema.json.
     */
    fun agentRegistered(agentId: UUID, tenantId: UUID, plan: String, createdAt: Instant = Instant.now()) {
        publishAgentKeyed(
            eventType = AuthTopics.AGENT_REGISTERED,
            agentId = agentId,
            tenantId = tenantId,
            payload = linkedMapOf(
                "agent_id" to agentId.toString(),
                "tenant_id" to tenantId.toString(),
                "created_at" to envelopes.iso(createdAt),
                "plan" to plan,
            ),
        )
    }

    /**
     * cypherx.auth.agent.updated — compact topic, keyed by agent_id (topics.md §4.1). Emitted on
     * agent state changes (status/scope updates); drives /authorize agent-cap cache invalidation.
     *
     * Event-fidelity fix (amended 2026-06): this previously mis-published to
     * `cypherx.auth.agent.deactivated`'s topic — agent.updated owns its OWN topic;
     * `agent.deactivated` ([AuthTopics.AGENT_DEACTIVATED]) stays reserved 📋 post-first-cycle.
     */
    fun agentUpdated(agentId: UUID, tenantId: UUID, status: String, updatedAt: Instant = Instant.now()) {
        publishAgentKeyed(
            eventType = AuthTopics.AGENT_UPDATED,
            agentId = agentId,
            tenantId = tenantId,
            payload = linkedMapOf(
                "agent_id" to agentId.toString(),
                "tenant_id" to tenantId.toString(),
                "status" to status,
                "updated_at" to envelopes.iso(updatedAt),
            ),
        )
    }

    // ── Internal helpers ─────────────────────────────────────────────────────────────────

    /**
     * Publish a compact agent event keyed by agent_id (topics.md §4.1). BOTH the Kafka message key
     * and the envelope partition_key are agent_id.
     */
    private fun publishAgentKeyed(eventType: String, agentId: UUID, tenantId: UUID, payload: Map<String, Any?>) {
        val key = agentId.toString()
        publish(eventType, key, tenantId, key, payload)
    }

    /**
     * Build the Contract 5 envelope and send it. Best-effort: serialization or broker failures are
     * logged at WARN and swallowed (the advisory event bus is never on the critical path).
     */
    private fun publish(
        eventType: String,
        messageKey: String,
        tenantId: UUID,
        partitionKey: String,
        payload: Map<String, Any?>,
    ) {
        val json = try {
            envelopes.json(eventType, tenantId, partitionKey, payload)
        } catch (ex: Exception) {
            log.warn("auth-event serialize failed for {} (event dropped): {}", eventType, ex.message)
            return
        }

        val template = kafka
        if (template == null) {
            log.info("kafka unavailable — auth-event {} (key={}) logged only: {}", eventType, messageKey, json)
            return
        }

        try {
            template.send(eventType, messageKey, json).whenComplete { _, ex ->
                if (ex != null) log.warn("auth-event {} (key={}) publish failed: {}", eventType, messageKey, ex.message)
            }
        } catch (ex: Exception) {
            // send() can throw synchronously if the producer cannot be created (no broker config).
            log.warn("auth-event {} (key={}) publish threw, event dropped: {}", eventType, messageKey, ex.message)
        }
    }

    private companion object {
        val log = LoggerFactory.getLogger(AuthEventPublisher::class.java)
    }
}
