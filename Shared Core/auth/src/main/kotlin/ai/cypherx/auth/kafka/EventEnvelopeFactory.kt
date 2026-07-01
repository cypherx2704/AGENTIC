package ai.cypherx.auth.kafka

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.PLATFORM_TENANT_ID
import ai.cypherx.auth.web.TraceContextFilter
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.MDC
import org.springframework.stereotype.Component
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Builds the canonical Contract 5 Kafka event envelope for the auth domain
 * (contracts/kafka/event-envelope.schema.json):
 *
 *     { event_id, event_type, schema_version, produced_at, trace_id, tenant_id,
 *       producer_service="auth", producer_version, partition_key, payload }
 *
 * Single source of truth for the envelope shape — used by BOTH publish paths:
 *  - [AuthEventPublisher] (direct best-effort, advisory topics), and
 *  - [OutboxEventWriter] (durable topics persisted to `auth.outbox` and drained by
 *    [OutboxRelay]).
 *
 * `trace_id` is lifted from the MDC ([TraceContextFilter]) so events correlate with the
 * request that caused them; a fresh id is generated for non-request contexts (jobs).
 */
@Component
class EventEnvelopeFactory(
    private val objectMapper: ObjectMapper,
    private val props: AuthProperties,
) {

    /** Build the envelope and serialize it to its on-the-wire JSON string. */
    fun json(eventType: String, tenantId: UUID, partitionKey: String, payload: Map<String, Any?>): String =
        objectMapper.writeValueAsString(envelope(eventType, tenantId, partitionKey, payload))

    /** Build the Contract 5 envelope as an ordered map (callers serialize / inspect). */
    fun envelope(
        eventType: String,
        tenantId: UUID,
        partitionKey: String,
        payload: Map<String, Any?>,
    ): Map<String, Any?> = linkedMapOf(
        "event_id" to UUID.randomUUID().toString(),
        "event_type" to eventType,
        "schema_version" to SCHEMA_VERSION,
        "produced_at" to iso(Instant.now()),
        "trace_id" to (MDC.get(TraceContextFilter.MDC_TRACE_ID) ?: UUID.randomUUID().toString().replace("-", "")),
        "tenant_id" to (tenantId.takeIf { it != ZERO_UUID } ?: PLATFORM_TENANT_ID).toString(),
        "producer_service" to PRODUCER_SERVICE,
        "producer_version" to props.version,
        "partition_key" to partitionKey,
        "payload" to payload,
    )

    /** ISO-8601 / RFC 3339 UTC with millisecond precision (Contract 5 `produced_at`). */
    fun iso(instant: Instant): String = TIMESTAMP_FMT.format(instant)

    companion object {
        const val PRODUCER_SERVICE = "auth"
        const val SCHEMA_VERSION = "1.0.0"

        private val ZERO_UUID: UUID = UUID.fromString("00000000-0000-0000-0000-000000000000")

        private val TIMESTAMP_FMT: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(java.time.ZoneOffset.UTC)
    }
}
