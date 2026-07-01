package ai.cypherx.auth.api

import ai.cypherx.auth.repo.WebhookRepository
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.WebhookService
import ai.cypherx.auth.web.ApiException
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.DeleteMapping
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Outbound-webhook management API (WP04, Contract 21):
 *
 *   POST   /v1/webhooks                      create a subscription (returns the signing secret ONCE)
 *   GET    /v1/webhooks                      list the caller-tenant's subscriptions
 *   DELETE /v1/webhooks/{id}                 delete a subscription (cascades its deliveries)
 *   POST   /v1/webhooks/{id}/rotate-secret   rotate the signing secret (returns the new secret ONCE)
 *   POST   /v1/webhooks/{id}/resume          un-pause a paused subscription
 *   POST   /v1/webhooks/{id}/replay          re-queue a past delivery ({ delivery_id })
 *   GET    /v1/webhooks/{id}/deliveries       list a subscription's deliveries
 *
 * Every route requires `tenant:admin` OR `platform:admin`, enforced in-handler via
 * [CallerContext.requireAnyScope] (method-level security is not enabled in the locked SecurityConfig;
 * the Core [ai.cypherx.auth.web.AgentJwtAuthFilter] attaches `SCOPE_*` authorities and SecurityConfig
 * already requires an authenticated principal for every webhooks route under `/v1/webhooks`). The tenant is taken
 * from the CALLER's verified JWT — never from the body (Contract 13). Errors are thrown as
 * [ApiException] → rendered by the Core GlobalExceptionHandler (Contract 2 envelope).
 *
 * The signing secret is surfaced exactly ONCE (on create + rotate-secret). It is stored only
 * envelope-encrypted and is never returned by any read endpoint.
 */
@RestController
@RequestMapping("/v1/webhooks")
class WebhookController(
    private val webhookService: WebhookService,
    private val callerContext: CallerContext,
) {

    /** POST /v1/webhooks — create. Body: { url, event_types: [...] }. Returns the secret ONCE. */
    @PostMapping
    fun create(@RequestBody(required = false) body: CreateWebhookRequest?): ResponseEntity<Map<String, Any?>> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        val url = body?.url
            ?: throw ApiException.validation("Missing required field: url", mapOf("field" to "url"))
        val eventTypes = body.eventTypes
            ?: throw ApiException.validation("Missing required field: event_types", mapOf("field" to "event_types"))

        val created = webhookService.create(caller.tenantId, url, eventTypes, caller.subject)
        return ResponseEntity.status(HttpStatus.CREATED).body(withSecret(created))
    }

    /** GET /v1/webhooks — list the caller-tenant's subscriptions (no secrets). */
    @GetMapping
    fun list(): Map<String, Any?> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        return mapOf("subscriptions" to webhookService.list(caller.tenantId).map(::subscriptionView))
    }

    /** DELETE /v1/webhooks/{id} — delete a subscription. 404 if absent. */
    @DeleteMapping("/{id}")
    fun delete(@PathVariable id: String): ResponseEntity<Void> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        webhookService.delete(caller.tenantId, parseUuid(id, "id"), caller.subject)
        return ResponseEntity.noContent().build()
    }

    /** POST /v1/webhooks/{id}/rotate-secret — rotate the signing secret. Returns the new secret ONCE. */
    @PostMapping("/{id}/rotate-secret")
    fun rotateSecret(@PathVariable id: String): Map<String, Any?> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        return withSecret(webhookService.rotateSecret(caller.tenantId, parseUuid(id, "id"), caller.subject))
    }

    /** POST /v1/webhooks/{id}/resume — un-pause a subscription. */
    @PostMapping("/{id}/resume")
    fun resume(@PathVariable id: String): Map<String, Any?> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        return subscriptionView(webhookService.resume(caller.tenantId, parseUuid(id, "id"), caller.subject))
    }

    /** POST /v1/webhooks/{id}/replay — re-queue a past delivery. Body: { delivery_id }. */
    @PostMapping("/{id}/replay")
    fun replay(
        @PathVariable id: String,
        @RequestBody(required = false) body: ReplayRequest?,
    ): ResponseEntity<Map<String, Any?>> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        val deliveryId = body?.deliveryId
            ?: throw ApiException.validation("Missing required field: delivery_id", mapOf("field" to "delivery_id"))
        val newId = webhookService.replay(
            caller.tenantId,
            parseUuid(id, "id"),
            parseUuid(deliveryId, "delivery_id"),
            caller.subject,
        )
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(
            mapOf("delivery_id" to newId.toString(), "status" to "pending"),
        )
    }

    /** GET /v1/webhooks/{id}/deliveries — list a subscription's deliveries (newest first). */
    @GetMapping("/{id}/deliveries")
    fun deliveries(
        @PathVariable id: String,
        @RequestParam(name = "limit", required = false, defaultValue = "50") limit: Int,
    ): Map<String, Any?> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        val rows = webhookService.listDeliveries(caller.tenantId, parseUuid(id, "id"), limit)
        return mapOf("deliveries" to rows.map(::deliveryView))
    }

    // ── View mapping ───────────────────────────────────────────────────────────────────────

    private fun withSecret(r: WebhookService.SubscriptionWithSecret): Map<String, Any?> =
        subscriptionView(r.subscription) + mapOf("signing_secret" to r.signingSecret)

    private fun subscriptionView(s: WebhookService.SubscriptionView): Map<String, Any?> = linkedMapOf(
        "sub_id" to s.subId.toString(),
        "url" to s.url,
        "event_types" to s.eventTypes,
        "status" to s.status,
        "created_at" to TIMESTAMP_FMT.format(s.createdAt),
    )

    private fun deliveryView(d: WebhookRepository.Delivery): Map<String, Any?> = linkedMapOf(
        "delivery_id" to d.deliveryId.toString(),
        "sub_id" to d.subId.toString(),
        "event_type" to d.eventType,
        "status" to d.status,
        "attempts" to d.attempts,
        "next_attempt_at" to TIMESTAMP_FMT.format(d.nextAttemptAt),
        "last_status_code" to d.lastStatusCode,
        "last_error" to d.lastError,
        "created_at" to TIMESTAMP_FMT.format(d.createdAt),
        "delivered_at" to d.deliveredAt?.let(TIMESTAMP_FMT::format),
    )

    private fun parseUuid(value: String?, field: String): UUID {
        if (value.isNullOrBlank()) {
            throw ApiException.validation("Missing required field: $field", mapOf("field" to field))
        }
        return runCatching { UUID.fromString(value) }.getOrElse {
            throw ApiException.validation("Invalid UUID for $field", mapOf("field" to field))
        }
    }

    // ── Request bodies ─────────────────────────────────────────────────────────────────────

    /** POST /v1/webhooks body. `event_types` may be `["*"]` for all events. */
    data class CreateWebhookRequest(
        val url: String? = null,
        val eventTypes: List<String>? = null,
    )

    /** POST /v1/webhooks/{id}/replay body. */
    data class ReplayRequest(
        val deliveryId: String? = null,
    )

    private companion object {
        const val SCOPE_TENANT_ADMIN = "tenant:admin"
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
        val TIMESTAMP_FMT: DateTimeFormatter = DateTimeFormatter.ISO_INSTANT
    }
}
