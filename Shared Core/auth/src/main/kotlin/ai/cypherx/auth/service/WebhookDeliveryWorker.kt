package ai.cypherx.auth.service

import ai.cypherx.auth.config.WebhookProperties
import ai.cypherx.auth.repo.WebhookRepository
import org.slf4j.LoggerFactory
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration
import java.time.Instant

/**
 * Signed-delivery worker for outbound webhooks (WP04, Contract 21).
 *
 * A fixed-delay loop ([tick], cadence `cypherx.auth.webhooks.poll-delay-ms`) drains
 * `auth.webhook_deliveries`: it fans out over active tenants ([WebhookRepository.activeTenantIds]),
 * reads each tenant's DUE deliveries (`pending`, or `failed` with `next_attempt_at <= now`) under
 * that tenant's RLS context, and POSTs each payload to its subscription URL with the Contract-21
 * signature headers:
 *
 *   X-Cypherx-Timestamp: <unix-seconds>
 *   X-Cypherx-Signature: hex(HMAC-SHA256(secret, "<timestamp>.<body>"))
 *   X-Cypherx-Event:     <event_type>
 *   X-Cypherx-Delivery:  <delivery_id>
 *
 * On a 2xx the delivery is marked `delivered`. On any other status / transport error the attempt
 * count is bumped and the delivery is rescheduled with exponential backoff
 * (`backoffBaseMs * 2^(attempts-1)`, capped at `backoffCapMs`); once `maxAttempts` is reached it is
 * marked `dead`. The whole loop is FAIL-SOFT — a per-delivery error is recorded on the row and the
 * loop moves on; a per-tenant or whole-pass error is logged and the next tick retries. Requires
 * `@EnableScheduling` (present on [ai.cypherx.auth.AuthApplication]).
 *
 * Disable cleanly with `cypherx.auth.webhooks.enabled=false` (tests do): [tick] no-ops and tests
 * drive delivery deterministically by calling [deliverOnce] themselves.
 */
@Component
class WebhookDeliveryWorker(
    private val repo: WebhookRepository,
    private val webhookService: WebhookService,
    private val props: WebhookProperties,
) {

    private val httpClient: HttpClient = HttpClient.newBuilder()
        .connectTimeout(Duration.ofMillis(props.connectTimeoutMs))
        .followRedirects(HttpClient.Redirect.NEVER)
        .build()

    /** Scheduled entrypoint. Honours the enable switch. */
    @Scheduled(fixedDelayString = "\${cypherx.auth.webhooks.poll-delay-ms:5000}")
    fun tick() {
        if (!props.enabled) return
        try {
            deliverOnce()
        } catch (ex: Exception) {
            // Whole-pass guard: never let a stray error kill the scheduler thread; retry next tick.
            log.warn("webhook delivery pass failed (will retry next tick): {}", ex.message)
        }
    }

    /**
     * Drain due deliveries across all active tenants now (also the deterministic test hook). Returns
     * the number of deliveries that ended `delivered` this pass.
     */
    fun deliverOnce(): Int {
        var delivered = 0
        val now = Instant.now()
        for (tenantId in repo.activeTenantIds()) {
            val due = runCatching { repo.dueDeliveries(tenantId, now, props.batchSize) }
                .onFailure { log.warn("reading due deliveries for tenant {} failed: {}", tenantId, it.message) }
                .getOrDefault(emptyList())
            for (delivery in due) {
                if (attempt(delivery)) delivered++
            }
        }
        if (delivered > 0) log.debug("webhook worker delivered {} delivery(ies)", delivered)
        return delivered
    }

    /** Deliver one row. Returns true on success (2xx). Records the outcome on the row either way. */
    private fun attempt(delivery: WebhookRepository.Delivery): Boolean {
        val tenantId = delivery.tenantId
        val sub = repo.findSubscription(tenantId, delivery.subId)
        if (sub == null) {
            // Subscription deleted out from under a queued delivery — terminal, nothing to deliver to.
            repo.markFailed(
                tenantId, delivery.deliveryId, delivery.attempts, null,
                "subscription no longer exists", Instant.now(), terminal = true,
            )
            return false
        }
        // A paused subscription should not be delivered to; reschedule (do not consume an attempt)
        // so a resume picks it back up. Push the next attempt one backoff window out.
        if (sub.status != "active") {
            repo.markFailed(
                tenantId, delivery.deliveryId, delivery.attempts, null,
                "subscription paused", nextBackoff(delivery.attempts.coerceAtLeast(1)), terminal = false,
            )
            return false
        }

        val attemptNo = delivery.attempts + 1
        val timestamp = Instant.now().epochSecond
        val signature = runCatching {
            val secret = webhookService.decryptSecret(sub.secretEnc)
            webhookService.computeSignature(secret, timestamp, delivery.payload)
        }.getOrElse {
            // Cannot sign (secret decrypt failed) — terminal; nothing we send would verify.
            log.warn("webhook sign failed delivery={} sub={}: {}", delivery.deliveryId, sub.subId, it.message)
            repo.markFailed(
                tenantId, delivery.deliveryId, attemptNo, null,
                "signing failed: ${it.message}", Instant.now(), terminal = true,
            )
            return false
        }

        return try {
            val request = HttpRequest.newBuilder()
                .uri(URI.create(sub.url))
                .timeout(Duration.ofMillis(props.requestTimeoutMs))
                .header("Content-Type", "application/json")
                .header("User-Agent", props.userAgent)
                .header(HEADER_TIMESTAMP, timestamp.toString())
                .header(HEADER_SIGNATURE, signature)
                .header(HEADER_EVENT, delivery.eventType)
                .header(HEADER_DELIVERY, delivery.deliveryId.toString())
                .POST(HttpRequest.BodyPublishers.ofString(delivery.payload))
                .build()

            val response = httpClient.send(request, HttpResponse.BodyHandlers.discarding())
            val code = response.statusCode()
            if (code in 200..299) {
                repo.markDelivered(tenantId, delivery.deliveryId, attemptNo, code, Instant.now())
                log.debug("webhook delivered delivery={} sub={} status={}", delivery.deliveryId, sub.subId, code)
                true
            } else {
                recordRetryableFailure(delivery, attemptNo, code, "non-2xx status $code")
                false
            }
        } catch (ex: Exception) {
            // Transport error (DNS / connect / timeout / TLS) — retryable.
            recordRetryableFailure(delivery, attemptNo, null, ex.message ?: ex.javaClass.simpleName)
            false
        }
    }

    /** Bump attempts; reschedule with backoff, or mark `dead` once [WebhookProperties.maxAttempts] is hit. */
    private fun recordRetryableFailure(
        delivery: WebhookRepository.Delivery,
        attemptNo: Int,
        statusCode: Int?,
        error: String,
    ) {
        val terminal = attemptNo >= props.maxAttempts
        val nextAttemptAt = if (terminal) Instant.now() else nextBackoff(attemptNo)
        repo.markFailed(
            delivery.tenantId, delivery.deliveryId, attemptNo, statusCode, error, nextAttemptAt, terminal,
        )
        if (terminal) {
            log.warn(
                "webhook delivery DEAD after {} attempts delivery={} sub={}: {}",
                attemptNo, delivery.deliveryId, delivery.subId, error,
            )
        } else {
            log.debug(
                "webhook delivery failed (attempt {}/{}) delivery={} sub={}: {} — retry at {}",
                attemptNo, props.maxAttempts, delivery.deliveryId, delivery.subId, error, nextAttemptAt,
            )
        }
    }

    /** Exponential backoff: `backoffBaseMs * 2^(attemptNo-1)`, capped at `backoffCapMs`, from now. */
    private fun nextBackoff(attemptNo: Int): Instant {
        val shift = (attemptNo - 1).coerceIn(0, MAX_SHIFT)
        val delayMs = (props.backoffBaseMs shl shift)
            .coerceAtMost(props.backoffCapMs)
            .coerceAtLeast(props.backoffBaseMs)
        return Instant.now().plusMillis(delayMs)
    }

    private companion object {
        const val HEADER_TIMESTAMP = "X-Cypherx-Timestamp"
        const val HEADER_SIGNATURE = "X-Cypherx-Signature"
        const val HEADER_EVENT = "X-Cypherx-Event"
        const val HEADER_DELIVERY = "X-Cypherx-Delivery"

        /** Guards the `shl` against overflow; the cap is the real ceiling. */
        const val MAX_SHIFT = 20

        val log = LoggerFactory.getLogger(WebhookDeliveryWorker::class.java)
    }
}
