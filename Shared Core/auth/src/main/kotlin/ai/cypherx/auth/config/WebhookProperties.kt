package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.webhooks.*` configuration tree (WP04 — outbound
 * webhooks + the signed-delivery worker, Contract 21).
 *
 * Bound automatically by @ConfigurationPropertiesScan on [ai.cypherx.auth.AuthApplication]. Every
 * value is env-overridable; the in-code values are the documented defaults — nothing here is a
 * hardcoded tunable.
 *
 * The worker ([ai.cypherx.auth.service.WebhookDeliveryWorker]) drains `auth.webhook_deliveries`:
 * it polls rows that are `pending`, or `failed` with `next_attempt_at <= now`, POSTs the payload to
 * the subscription URL with a Contract-21 HMAC-SHA256 signature header, and on failure reschedules
 * with exponential backoff (`backoffBaseMs * 2^attempts`, capped at [backoffCapMs]) until
 * [maxAttempts] is reached, after which the delivery is marked `dead` (fail-soft — the loop never
 * throws; a transient error is logged and retried next tick).
 */
@ConfigurationProperties(prefix = "cypherx.auth.webhooks")
data class WebhookProperties(

    /** Master switch for the delivery worker loop. Tests disable it and drain explicitly. */
    val enabled: Boolean = true,

    /** Fixed delay between delivery-worker polls (ms). */
    val pollDelayMs: Long = 5_000,

    /** Max due deliveries drained per worker pass (oldest first). */
    val batchSize: Int = 100,

    /** Per-delivery HTTP connect timeout (ms) when opening the connection to the subscriber. */
    val connectTimeoutMs: Long = 5_000,

    /** Per-delivery HTTP request/response timeout (ms). */
    val requestTimeoutMs: Long = 10_000,

    /** Total delivery attempts before a delivery is marked `dead`. */
    val maxAttempts: Int = 8,

    /** Backoff base (ms): the n-th retry is scheduled `backoffBaseMs * 2^(attempts-1)` from now. */
    val backoffBaseMs: Long = 30_000,

    /** Backoff ceiling (ms) — a single retry never waits longer than this. */
    val backoffCapMs: Long = 3_600_000,

    /**
     * Number of bytes of randomness for a freshly-generated signing secret (hex-encoded for the
     * caller). 32 bytes = 256 bits, matching the HMAC-SHA256 key size.
     */
    val secretBytes: Int = 32,

    /**
     * Value of the `User-Agent` header sent with every delivery, so subscribers can recognise
     * CypherX webhook traffic.
     */
    val userAgent: String = "CypherX-Webhooks/1.0",
)
