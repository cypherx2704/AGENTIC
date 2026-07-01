package ai.cypherx.auth.kafka

import ai.cypherx.auth.config.OutboxProperties
import ai.cypherx.auth.repo.OutboxRepository
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.ObjectProvider
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component
import java.time.Instant
import java.util.concurrent.TimeUnit

/**
 * Drains `auth.outbox` to Kafka (Phase 2 Amendment Log 2026-06 / WP02).
 *
 * A fixed-delay loop ([tick], cadence `cypherx.auth.outbox.relay-delay-ms`, default 1s — well
 * under the ≤5s staleness SLA) polls unpublished rows oldest-first (batch size from config),
 * publishes each via the shared [KafkaTemplate] (synchronous send, timeout from config), and
 * stamps `published_at`. A failed send increments `attempts` + `last_error` on the row and the
 * row is retried FOREVER — there is no drop and no DLQ for provisioning-critical events; when an
 * entire pass fails (broker down) the loop backs off exponentially up to
 * `cypherx.auth.outbox.backoff-cap-ms`, resetting on the first success.
 *
 * Delivery is at-least-once (a crash between send and stamp re-publishes); consumers
 * de-duplicate on the envelope `event_id`.
 *
 * Disable cleanly with `cypherx.auth.outbox.enabled=false` (the test profile does): [tick]
 * no-ops, and tests drain deterministically by calling [relayOnce] themselves.
 *
 * If no [KafkaTemplate] bean exists at all (broker-less local boot), rows simply accumulate
 * until a broker is configured — durability is never traded away.
 */
@Component
class OutboxRelay(
    private val outboxRepository: OutboxRepository,
    private val props: OutboxProperties,
    kafkaTemplateProvider: ObjectProvider<KafkaTemplate<String, String>>,
) {

    /** Resolved once; null when Kafka auto-config produced no template (rows accumulate). */
    private val kafka: KafkaTemplate<String, String>? = kafkaTemplateProvider.ifAvailable

    /** Backoff gate: [tick] skips passes until this instant. EPOCH = no backoff. */
    @Volatile
    private var nextAttemptAt: Instant = Instant.EPOCH

    @Volatile
    private var failureStreak: Int = 0

    /** Current backoff gate (observability / tests). EPOCH = gate open. */
    fun nextAttemptAt(): Instant = nextAttemptAt

    /** Scheduled entrypoint. Honours the enable switch and the failure-backoff gate. */
    @Scheduled(fixedDelayString = "\${cypherx.auth.outbox.relay-delay-ms:1000}")
    fun tick() {
        if (!props.enabled) return
        if (Instant.now().isBefore(nextAttemptAt)) return
        relayOnce()
    }

    /**
     * Drain one batch now (also the deterministic test hook — bypasses the backoff gate).
     * Returns the number of rows successfully published.
     */
    fun relayOnce(): Int {
        val template = kafka
        if (template == null) {
            log.debug("outbox relay idle — no KafkaTemplate configured")
            return 0
        }

        val rows = outboxRepository.fetchUnpublished(props.batchSize)
        if (rows.isEmpty()) {
            recordSuccess()
            return 0
        }

        var published = 0
        var failed = 0
        for (row in rows) {
            try {
                template.send(row.topic, row.partitionKey, row.payloadJson)
                    .get(props.sendTimeoutMs, TimeUnit.MILLISECONDS)
                outboxRepository.markPublished(row.id)
                published++
            } catch (ex: Exception) {
                // Per-row failure: record it and move on; the row stays unpublished and retries.
                outboxRepository.markFailed(row.id, ex.message ?: ex.javaClass.simpleName)
                failed++
                log.warn(
                    "outbox publish failed for {} row {} (attempt {}): {}",
                    row.topic, row.id, row.attempts + 1, ex.message,
                )
            }
        }

        if (failed > 0 && published == 0) recordFailure() else recordSuccess()
        if (published > 0) log.debug("outbox relay published {} row(s)", published)
        return published
    }

    /** Whole pass failed (broker down): double the gate, capped at the configured ceiling. */
    private fun recordFailure() {
        failureStreak = (failureStreak + 1).coerceAtMost(MAX_TRACKED_STREAK)
        val delayMs = (props.relayDelayMs shl (failureStreak - 1).coerceAtMost(MAX_SHIFT))
            .coerceAtMost(props.backoffCapMs)
            .coerceAtLeast(props.relayDelayMs)
        nextAttemptAt = Instant.now().plusMillis(delayMs)
        log.warn("outbox relay backing off {} ms after {} consecutive failed pass(es)", delayMs, failureStreak)
    }

    private fun recordSuccess() {
        failureStreak = 0
        nextAttemptAt = Instant.EPOCH
    }

    private companion object {
        /** Streak/shift bounds only guard arithmetic overflow — the cap is the real ceiling. */
        const val MAX_TRACKED_STREAK = 30
        const val MAX_SHIFT = 20

        val log = LoggerFactory.getLogger(OutboxRelay::class.java)
    }
}
