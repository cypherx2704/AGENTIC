package ai.cypherx.auth.service

import ai.cypherx.auth.config.WebhookProperties
import ai.cypherx.auth.crypto.KeyEncryptor
import ai.cypherx.auth.repo.WebhookRepository
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.nio.charset.StandardCharsets
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import java.time.Instant
import java.util.UUID

/**
 * Outbound-webhook management + enqueue (WP04, Contract 21).
 *
 * Owns the lifecycle of `auth.webhook_subscriptions` and the enqueue side of
 * `auth.webhook_deliveries`; the actual HTTP delivery + retry is the
 * [ai.cypherx.auth.service.WebhookDeliveryWorker]'s job.
 *
 * Signing secret handling mirrors the signing-key pattern: a fresh secret is generated as
 * [WebhookProperties.secretBytes] of [SecureRandom], returned to the caller ONCE (hex) at create /
 * rotate-secret, and persisted only envelope-encrypted via [KeyEncryptor] (`secret_enc BYTEA`). The
 * clear secret is never stored or logged. The same secret keys the Contract-21 HMAC-SHA256 the
 * worker stamps on every delivery so the subscriber can verify authenticity.
 *
 * [enqueue] is the self-contained integration point: any feature that emits an event the platform
 * wants to fan out to tenant webhooks (audit, onboarding, a future Kafka consumer) calls it with the
 * tenant, event type, and JSON-serialisable payload; it expands the event against the tenant's
 * matching active subscriptions and inserts one `pending` delivery per match. Enqueue is best-effort
 * fail-soft — a webhook failure never breaks the originating operation.
 *
 * Scope enforcement (tenant:admin OR platform:admin) and tenant resolution live in
 * [ai.cypherx.auth.api.WebhookController] via [CallerContext]; this service trusts the tenant id it
 * is handed (RLS in [WebhookRepository] is the hard boundary).
 */
@Service
class WebhookService(
    private val repo: WebhookRepository,
    private val encryptor: KeyEncryptor,
    private val auditService: AuditService,
    private val objectMapper: ObjectMapper,
    private val props: WebhookProperties,
) {

    private val secureRandom = SecureRandom()

    /** A subscription as surfaced to the API (never carries the secret). */
    data class SubscriptionView(
        val subId: UUID,
        val url: String,
        val eventTypes: List<String>,
        val status: String,
        val createdAt: Instant,
    )

    /** Result of create / rotate-secret: the view PLUS the clear signing secret, shown ONCE. */
    data class SubscriptionWithSecret(val subscription: SubscriptionView, val signingSecret: String)

    // ── Subscription lifecycle ─────────────────────────────────────────────────────────────

    /**
     * Create a subscription for [tenantId]. Generates + returns the signing secret ONCE; persists it
     * only encrypted. [eventTypes] must be non-empty (use a single-element `["*"]` for all events).
     */
    fun create(tenantId: UUID, url: String, eventTypes: List<String>, actor: String?): SubscriptionWithSecret {
        val cleanUrl = validateUrl(url)
        val types = normaliseEventTypes(eventTypes)
        val secret = generateSecret()
        val secretEnc = encryptor.encrypt(secret.toByteArray(StandardCharsets.UTF_8))

        val row = repo.insertSubscription(tenantId, cleanUrl, types, secretEnc)
        audit(tenantId, actor, "webhook:create", "webhook:${row.subId}")
        log.info("webhook subscription created sub={} tenant={} events={}", row.subId, tenantId, types)
        return SubscriptionWithSecret(view(row), secret)
    }

    /** List the caller-tenant's subscriptions (no secrets). */
    fun list(tenantId: UUID): List<SubscriptionView> =
        repo.listSubscriptions(tenantId).map(::view)

    /** Delete a subscription (and, by FK cascade, its deliveries). 404 if it does not exist. */
    fun delete(tenantId: UUID, subId: UUID, actor: String?) {
        if (!repo.deleteSubscription(tenantId, subId)) {
            throw ApiException.notFound("Webhook subscription not found", mapOf("sub_id" to subId.toString()))
        }
        audit(tenantId, actor, "webhook:delete", "webhook:$subId")
        log.info("webhook subscription deleted sub={} tenant={}", subId, tenantId)
    }

    /** Rotate a subscription's signing secret. Returns the NEW clear secret once. 404 if absent. */
    fun rotateSecret(tenantId: UUID, subId: UUID, actor: String?): SubscriptionWithSecret {
        val existing = repo.findSubscription(tenantId, subId)
            ?: throw ApiException.notFound("Webhook subscription not found", mapOf("sub_id" to subId.toString()))
        val secret = generateSecret()
        val secretEnc = encryptor.encrypt(secret.toByteArray(StandardCharsets.UTF_8))
        repo.updateSecret(tenantId, subId, secretEnc)
        audit(tenantId, actor, "webhook:rotate-secret", "webhook:$subId")
        log.info("webhook signing secret rotated sub={} tenant={}", subId, tenantId)
        return SubscriptionWithSecret(view(existing), secret)
    }

    /** Un-pause a subscription (status -> active). 404 if absent. */
    fun resume(tenantId: UUID, subId: UUID, actor: String?): SubscriptionView {
        if (!repo.updateStatus(tenantId, subId, "active")) {
            throw ApiException.notFound("Webhook subscription not found", mapOf("sub_id" to subId.toString()))
        }
        audit(tenantId, actor, "webhook:resume", "webhook:$subId")
        val row = repo.findSubscription(tenantId, subId)
            ?: throw ApiException.notFound("Webhook subscription not found", mapOf("sub_id" to subId.toString()))
        log.info("webhook subscription resumed sub={} tenant={}", subId, tenantId)
        return view(row)
    }

    // ── Deliveries (replay + read) ───────────────────────────────────────────────────────────

    /** List a subscription's deliveries (newest first). 404 if the subscription is absent. */
    fun listDeliveries(tenantId: UUID, subId: UUID, limit: Int): List<WebhookRepository.Delivery> {
        repo.findSubscription(tenantId, subId)
            ?: throw ApiException.notFound("Webhook subscription not found", mapOf("sub_id" to subId.toString()))
        return repo.listDeliveries(tenantId, subId, limit.coerceIn(1, MAX_LIST_LIMIT))
    }

    /**
     * Re-queue a past delivery for [subId] as a fresh pending delivery (the original is preserved as
     * the historical record). 404 if the source delivery does not exist for this tenant.
     */
    fun replay(tenantId: UUID, subId: UUID, deliveryId: UUID, actor: String?): UUID {
        // Confirm the source delivery belongs to this subscription (and tenant) before replaying.
        val source = repo.findDelivery(tenantId, deliveryId)
        if (source == null || source.subId != subId) {
            throw ApiException.notFound(
                "Delivery not found for this subscription",
                mapOf("sub_id" to subId.toString(), "delivery_id" to deliveryId.toString()),
            )
        }
        val newId = repo.replayDelivery(tenantId, deliveryId)
            ?: throw ApiException.notFound("Delivery not found", mapOf("delivery_id" to deliveryId.toString()))
        audit(tenantId, actor, "webhook:replay", "delivery:$deliveryId")
        log.info("webhook delivery replayed source={} new={} sub={} tenant={}", deliveryId, newId, subId, tenantId)
        return newId
    }

    /**
     * Re-queue every recently-FAILED delivery for [subId] as a fresh pending delivery — the backing for
     * the "replay recent failures" action (`POST …/replay` with no `delivery_id`). Fail-soft per delivery;
     * returns the number re-queued. 404 if the subscription is absent for this tenant.
     */
    fun replayRecentFailures(tenantId: UUID, subId: UUID, actor: String?): Int {
        repo.findSubscription(tenantId, subId)
            ?: throw ApiException.notFound("Webhook subscription not found", mapOf("sub_id" to subId.toString()))
        val failed = repo.listDeliveries(tenantId, subId, MAX_LIST_LIMIT).filter { it.status == "failed" }
        var replayed = 0
        for (d in failed) {
            runCatching { repo.replayDelivery(tenantId, d.deliveryId) }
                .onSuccess { if (it != null) replayed++ }
                .onFailure { log.warn("webhook replay-failure enqueue failed delivery={}: {}", d.deliveryId, it.message) }
        }
        if (replayed > 0) audit(tenantId, actor, "webhook:replay-failures", "webhook:$subId")
        log.info("webhook replay-recent-failures sub={} tenant={} replayed={}", subId, tenantId, replayed)
        return replayed
    }

    // ── Enqueue (integration point for event producers) ──────────────────────────────────────

    /**
     * Expand [eventType] against [tenantId]'s matching ACTIVE subscriptions and enqueue one pending
     * delivery per match. Self-contained and FAIL-SOFT: returns the number of deliveries queued; any
     * failure is logged and swallowed so the originating operation is never broken by webhook fan-out.
     *
     * Producers (audit / onboarding / a future Kafka consumer) call this with a JSON-serialisable
     * [payload]; the serialised JSON is what the worker signs and POSTs verbatim.
     */
    fun enqueue(tenantId: UUID, eventType: String, payload: Any): Int {
        return runCatching {
            val subs = repo.matchingActiveSubscriptions(tenantId, eventType)
            if (subs.isEmpty()) return 0
            val payloadJson = objectMapper.writeValueAsString(payload)
            var queued = 0
            for (sub in subs) {
                runCatching { repo.insertDelivery(tenantId, sub.subId, eventType, payloadJson) }
                    .onSuccess { queued++ }
                    .onFailure { log.warn("webhook enqueue failed sub={} event={}: {}", sub.subId, eventType, it.message) }
            }
            if (queued > 0) log.debug("enqueued {} webhook delivery(ies) event={} tenant={}", queued, eventType, tenantId)
            queued
        }.onFailure {
            log.warn("webhook enqueue pass failed tenant={} event={}: {}", tenantId, eventType, it.message)
        }.getOrDefault(0)
    }

    // ── Signing (shared with the worker) ─────────────────────────────────────────────────────

    /** Decrypt a subscription's stored signing secret back to its clear UTF-8 form (worker-internal). */
    fun decryptSecret(secretEnc: ByteArray): String =
        String(encryptor.decrypt(secretEnc), StandardCharsets.UTF_8)

    /**
     * Contract-21 delivery signature: `hex(HMAC-SHA256(secret, "<timestamp>.<body>"))`. The
     * `X-Cypherx-Timestamp` header carries [timestampSeconds] and `X-Cypherx-Signature` carries this
     * hex digest; the subscriber recomputes it over the same `timestamp + "." + body` to authenticate
     * and bound replay. Timestamp and body are joined with a '.' separator so the two fields cannot be
     * ambiguously concatenated.
     */
    fun computeSignature(secret: String, timestampSeconds: Long, body: String): String {
        val mac = Mac.getInstance(HMAC_ALG)
        mac.init(SecretKeySpec(secret.toByteArray(StandardCharsets.UTF_8), HMAC_ALG))
        val signed = "$timestampSeconds.$body"
        return mac.doFinal(signed.toByteArray(StandardCharsets.UTF_8)).toHex()
    }

    // ── Internals ────────────────────────────────────────────────────────────────────────────

    /** A URL-safe, random hex signing secret of [WebhookProperties.secretBytes] bytes. */
    private fun generateSecret(): String {
        val bytes = ByteArray(props.secretBytes.coerceAtLeast(16))
        secureRandom.nextBytes(bytes)
        return bytes.toHex()
    }

    /** Validate the subscriber URL is an absolute http(s) URL; reject everything else (SSRF guard). */
    private fun validateUrl(url: String): String {
        val trimmed = url.trim()
        if (trimmed.isEmpty()) {
            throw ApiException.validation("Missing required field: url", mapOf("field" to "url"))
        }
        val lower = trimmed.lowercase()
        if (!lower.startsWith("https://") && !lower.startsWith("http://")) {
            throw ApiException.validation(
                "url must be an absolute http(s) URL",
                mapOf("field" to "url"),
            )
        }
        return trimmed
    }

    /** Require at least one event-type filter; trim/dedupe; allow '*' as the all-events wildcard. */
    private fun normaliseEventTypes(eventTypes: List<String>): List<String> {
        val cleaned = eventTypes.map { it.trim() }.filter { it.isNotEmpty() }.distinct()
        if (cleaned.isEmpty()) {
            throw ApiException.validation(
                "event_types must contain at least one event type (use [\"*\"] for all)",
                mapOf("field" to "event_types"),
            )
        }
        return cleaned
    }

    private fun view(row: WebhookRepository.Subscription) = SubscriptionView(
        subId = row.subId,
        url = row.url,
        eventTypes = row.eventTypes,
        status = row.status,
        createdAt = row.createdAt,
    )

    /** Durable audit row (Component 6). Best-effort — never aborts the webhook operation. */
    private fun audit(tenantId: UUID, actor: String?, action: String, resource: String) {
        runCatching {
            auditService.record(
                eventType = "webhook.config",
                tenantId = tenantId,
                agentId = actor?.let { runCatching { UUID.fromString(it) }.getOrNull() },
                action = action,
                resource = resource,
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for {} {}: {}", action, resource, it.message) }
    }

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

    private companion object {
        const val HMAC_ALG = "HmacSHA256"
        const val MAX_LIST_LIMIT = 200
        val log = LoggerFactory.getLogger(WebhookService::class.java)
    }
}
