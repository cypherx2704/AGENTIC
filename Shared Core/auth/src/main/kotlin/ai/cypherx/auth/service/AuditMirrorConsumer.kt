package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuditPipelineProperties
import ai.cypherx.auth.service.s3.ObjectStore
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.LoggerFactory
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty
import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * Kafka consumer that MIRRORS the durable audit-append stream to object storage (S3/MinIO in prod,
 * local filesystem in dev) — an off-box, append-only copy of the tamper-evident audit trail
 * (Component 6 — WP04).
 *
 * Consumed topic:  `cypherx.auth.audit.appended`
 *   (env `CYPHERX_AUTH_AUDIT_PIPELINE_AUDIT_MIRROR_TOPIC`)
 * Consumer group:  `auth-audit-mirror`
 *   (env `CYPHERX_AUTH_AUDIT_PIPELINE_AUDIT_MIRROR_GROUP_ID`)
 *
 * NOTE on the topic: auth-service does not yet PUBLISH a dedicated audit-append event in the
 * first cycle (audit rows are written directly by [AuditService] inside the request tx; there is no
 * `cypherx.auth.audit.appended` producer in [ai.cypherx.auth.kafka.AuthTopics] yet). This consumer
 * therefore documents and mirrors the RESERVED topic `cypherx.auth.audit.appended` — once Auth emits
 * that event (outbox-routed, keyed by tenant_id), this mirror lands every row to object storage with
 * NO code change. Until then it is enabled only where that producer exists.
 *
 * Each mirrored record is written as a single object under
 * `{mirrorKeyPrefix}/{tenant}/{yyyy}/{MM}/{dd}/{id-or-ts}.json` so the mirror is partitioned by
 * tenant + date for cheap lifecycle/retention rules on the bucket.
 *
 * FAIL-SOFT (WP04): the bean exists ONLY when
 * `cypherx.auth.audit-pipeline.audit-mirror.enabled=true` (OFF by default), so a broker-less boot /
 * the test profile starts with no listener. Additionally, if the [ObjectStore] is unconfigured
 * ([ObjectStore.isConfigured] == false) each record is logged and skipped rather than throwing — the
 * mirror is a best-effort durability copy, never on the audit-write critical path (the DB row is the
 * system of record).
 */
@Component
@ConditionalOnProperty(prefix = "cypherx.auth.audit-pipeline.audit-mirror", name = ["enabled"], havingValue = "true")
class AuditMirrorConsumer(
    private val objectStore: ObjectStore,
    private val props: AuditPipelineProperties,
    private val objectMapper: ObjectMapper,
) {

    /**
     * Mirror one audit-append record. [message] is the Contract 5 envelope JSON. We persist the whole
     * envelope (so the mirror is self-describing) under a tenant/date-partitioned key derived from the
     * envelope. Failures are logged and swallowed (the mirror is best-effort).
     */
    @KafkaListener(
        topics = ["\${cypherx.auth.audit-pipeline.audit-mirror.topic:cypherx.auth.audit.appended}"],
        groupId = "\${cypherx.auth.audit-pipeline.audit-mirror.group-id:auth-audit-mirror}",
    )
    fun onAuditAppended(message: String) {
        if (!objectStore.isConfigured) {
            log.debug("audit mirror: object store not configured ({}), record skipped", objectStore.backend)
            return
        }
        val root: JsonNode = try {
            objectMapper.readTree(message)
        } catch (ex: Exception) {
            log.warn("audit mirror: record is not valid JSON — skipped: {}", ex.message)
            return
        }

        val key = buildKey(root)
        try {
            objectStore.putBytes(key, message.toByteArray(Charsets.UTF_8), CONTENT_TYPE_JSON)
            log.debug("audit mirror wrote {} ({})", key, objectStore.backend)
        } catch (ex: ObjectStore.ObjectStoreException) {
            log.warn("audit mirror write failed for key {} — skipped: {}", key, ex.message)
        }
    }

    /**
     * Derive a tenant + date-partitioned object key from the envelope. Falls back to `unknown`/now
     * when the envelope omits a field, so a slightly-off record is still mirrored (never dropped for
     * a missing partition hint).
     */
    private fun buildKey(root: JsonNode): String {
        val payload = root.path("payload")
        val tenant = root.path("tenant_id").asText(null)
            ?: payload.path("tenant_id").asText(null)
            ?: "unknown"
        val ts = root.path("produced_at").asText(null)
            ?.let { runCatching { Instant.parse(it) }.getOrNull() }
            ?: Instant.now()
        val id = payload.path("id").asText(null)
            ?: root.path("event_id").asText(null)
            ?: ts.toEpochMilli().toString()
        val datePath = DATE_PATH_FMT.format(ts)
        val safeId = id.replace(Regex("[^A-Za-z0-9_.-]"), "_")
        return "${props.mirrorKeyPrefix}/$tenant/$datePath/$safeId.json"
    }

    private companion object {
        const val CONTENT_TYPE_JSON = "application/json"

        val DATE_PATH_FMT: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyy/MM/dd").withZone(ZoneOffset.UTC)

        val log = LoggerFactory.getLogger(AuditMirrorConsumer::class.java)
    }
}
