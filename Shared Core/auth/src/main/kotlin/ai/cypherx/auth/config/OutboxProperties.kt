package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.outbox.*` configuration tree
 * (see src/main/resources/application.yaml) — the transactional-outbox relay knobs
 * (Phase 2 Amendment Log 2026-06 / WP02).
 *
 * Bound automatically by @ConfigurationPropertiesScan on [ai.cypherx.auth.AuthApplication].
 * Every value is env-overridable; nothing here is a hardcoded tunable.
 */
@ConfigurationProperties(prefix = "cypherx.auth.outbox")
data class OutboxProperties(

    /** Master switch for the relay loop. Tests disable it and drain the outbox explicitly. */
    val enabled: Boolean = true,

    /** Fixed delay between relay polls (ms). Bounds event staleness — keep well under the 5s SLA. */
    val relayDelayMs: Long = 1000,

    /** Max unpublished rows drained per relay pass (oldest first). */
    val batchSize: Int = 100,

    /** Per-record synchronous Kafka send timeout (ms). */
    val sendTimeoutMs: Long = 10_000,

    /** Backoff ceiling (ms) when every send in a pass fails (broker down). Retries forever. */
    val backoffCapMs: Long = 60_000,
)
