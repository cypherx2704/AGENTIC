package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuditPipelineProperties
import org.slf4j.LoggerFactory
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty
import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

/**
 * Kafka consumer that rolls `cypherx.llms.usage.recorded` (Contract 19) into
 * `auth.tenant_usage_counters` — the rollup the `/v1/usage` endpoint reads (Component 1d / WP04).
 *
 * Consumed topic:  `cypherx.llms.usage.recorded`
 *   (env `CYPHERX_AUTH_AUDIT_PIPELINE_USAGE_ROLLUP_TOPIC`)
 * Consumer group:  `auth-usage-rollup`
 *   (env `CYPHERX_AUTH_AUDIT_PIPELINE_USAGE_ROLLUP_GROUP_ID`)
 *
 * FAIL-SOFT (WP04): this bean exists ONLY when `cypherx.auth.audit-pipeline.usage-rollup.enabled=true`
 * (env `CYPHERX_AUTH_AUDIT_PIPELINE_USAGE_ROLLUP_ENABLED=true`). It is OFF by default, so a
 * broker-less local boot AND the test profile (which excludes KafkaAutoConfiguration entirely) start
 * with NO listener container — nothing to connect, nothing to fail. Enable it only where a broker is
 * configured. Delivery is at-least-once; the rollup UPSERT is additive, so a redelivered event would
 * double-count — production should run this with a de-dup window once Contract 19's `event_id` carry
 * is wired (documented gap; the rollup is advisory analytics, not an enforcement gate).
 *
 * A malformed/poison record is logged and skipped by [UsageRollupService.applyEnvelopeJson] rather
 * than failing the partition.
 */
@Component
@ConditionalOnProperty(prefix = "cypherx.auth.audit-pipeline.usage-rollup", name = ["enabled"], havingValue = "true")
class UsageRollupConsumer(
    private val usageRollupService: UsageRollupService,
    @Suppress("unused") private val props: AuditPipelineProperties,
) {

    /**
     * Consume one usage event. The String value is the Contract 5 envelope JSON; the rollup logic
     * lives in [UsageRollupService]. Exceptions are swallowed (logged) so one bad record cannot
     * stall the rollup partition (the rollup is advisory analytics).
     */
    @KafkaListener(
        topics = ["\${cypherx.auth.audit-pipeline.usage-rollup.topic:cypherx.llms.usage.recorded}"],
        groupId = "\${cypherx.auth.audit-pipeline.usage-rollup.group-id:auth-usage-rollup}",
    )
    fun onUsageEvent(message: String) {
        try {
            usageRollupService.applyEnvelopeJson(message)
        } catch (ex: Exception) {
            log.warn("usage rollup failed for a record (skipped): {}", ex.message)
        }
    }

    private companion object {
        val log = LoggerFactory.getLogger(UsageRollupConsumer::class.java)
    }
}
