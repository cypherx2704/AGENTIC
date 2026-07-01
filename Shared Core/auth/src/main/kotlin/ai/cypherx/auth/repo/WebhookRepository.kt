package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.ConnectionCallback
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * Tenant-scoped persistence for `auth.webhook_subscriptions` and `auth.webhook_deliveries`
 * (WP04 — outbound webhooks, Contract 21).
 *
 * Both tables are TENANT-SCOPED (RLS `USING (tenant_id = app.tenant_id)`), so every access goes
 * through [TenantTx.inTenant], which sets `app.tenant_id` for PostgreSQL RLS (Contract 13). NO JPA
 * — plain JdbcTemplate on the tx-bound connection, matching [AgentRepository] / [ApiKeyRepository].
 *
 * `event_types` is `TEXT[]` — passed as a real `java.sql.Array`. `payload` is JSONB — passed as
 * text and cast `::jsonb`. `secret_enc` is BYTEA — the envelope-encrypted HMAC secret bytes (the
 * service encrypts with [ai.cypherx.auth.crypto.KeyEncryptor]; the raw secret is never stored).
 *
 * The delivery worker reads due rows across ALL tenants; because the deliveries table is RLS-scoped,
 * [dueDeliveries] first enumerates tenants with due work via [tenantsWithDueDeliveries] (a platform
 * read that bypasses RLS predicates only for the tenant_id projection), then reads each tenant's due
 * rows inside that tenant's transaction. State transitions are likewise tenant-scoped.
 */
@Repository
class WebhookRepository(
    private val tenantTx: TenantTx,
) {

    // ── Row projections ──────────────────────────────────────────────────────────────────

    /** A `auth.webhook_subscriptions` row. `secretEnc` is the envelope-encrypted HMAC secret. */
    data class Subscription(
        val subId: UUID,
        val tenantId: UUID,
        val url: String,
        val eventTypes: List<String>,
        val secretEnc: ByteArray,
        val status: String,
        val createdAt: Instant,
    ) {
        // ByteArray breaks data-class equality; compare by sub_id (the identity), per SigningKey.
        override fun equals(other: Any?): Boolean = this === other || (other is Subscription && other.subId == subId)
        override fun hashCode(): Int = subId.hashCode()
    }

    /** A `auth.webhook_deliveries` row as the worker / list endpoint sees it. */
    data class Delivery(
        val deliveryId: UUID,
        val subId: UUID,
        val tenantId: UUID,
        val eventType: String,
        val payload: String,
        val status: String,
        val attempts: Int,
        val nextAttemptAt: Instant,
        val lastStatusCode: Int?,
        val lastError: String?,
        val createdAt: Instant,
        val deliveredAt: Instant?,
    )

    // ── Subscriptions ──────────────────────────────────────────────────────────────────────

    /** Insert a new subscription and return the persisted row. */
    fun insertSubscription(
        tenantId: UUID,
        url: String,
        eventTypes: List<String>,
        secretEnc: ByteArray,
    ): Subscription = tenantTx.inTenant(tenantId) { jdbc ->
        val typesArray = jdbc.execute(
            ConnectionCallback { con -> con.createArrayOf("text", eventTypes.toTypedArray()) },
        )
        jdbc.queryForObject(
            """
            INSERT INTO auth.webhook_subscriptions (tenant_id, url, event_types, secret_enc)
            VALUES (?, ?, ?, ?)
            RETURNING sub_id, tenant_id, url, event_types, secret_enc, status, created_at
            """.trimIndent(),
            SUBSCRIPTION_MAPPER,
            tenantId,
            url,
            typesArray,
            secretEnc,
        ) ?: error("INSERT ... RETURNING produced no row for webhook subscription")
    }

    /** List every subscription for [tenantId], newest first. */
    fun listSubscriptions(tenantId: UUID): List<Subscription> = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.query(
            """
            SELECT sub_id, tenant_id, url, event_types, secret_enc, status, created_at
              FROM auth.webhook_subscriptions
             ORDER BY created_at DESC
            """.trimIndent(),
            SUBSCRIPTION_MAPPER,
        )
    }

    /** Find one subscription by id within [tenantId]; null when absent (or RLS-invisible). */
    fun findSubscription(tenantId: UUID, subId: UUID): Subscription? = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.query(
            """
            SELECT sub_id, tenant_id, url, event_types, secret_enc, status, created_at
              FROM auth.webhook_subscriptions
             WHERE sub_id = ?
            """.trimIndent(),
            SUBSCRIPTION_MAPPER,
            subId,
        ).firstOrNull()
    }

    /** Active subscriptions whose `event_types` match [eventType] (or contain the '*' wildcard). */
    fun matchingActiveSubscriptions(tenantId: UUID, eventType: String): List<Subscription> =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                SELECT sub_id, tenant_id, url, event_types, secret_enc, status, created_at
                  FROM auth.webhook_subscriptions
                 WHERE status = 'active'
                   AND (event_types @> ARRAY[?]::text[] OR event_types @> ARRAY['*']::text[])
                """.trimIndent(),
                SUBSCRIPTION_MAPPER,
                eventType,
            )
        }

    /** Delete a subscription (cascades to its deliveries). Returns true when a row was removed. */
    fun deleteSubscription(tenantId: UUID, subId: UUID): Boolean = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.update("DELETE FROM auth.webhook_subscriptions WHERE sub_id = ?", subId) > 0
    }

    /** Replace a subscription's encrypted secret (rotate-secret). Returns true when a row was updated. */
    fun updateSecret(tenantId: UUID, subId: UUID, secretEnc: ByteArray): Boolean =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                "UPDATE auth.webhook_subscriptions SET secret_enc = ? WHERE sub_id = ?",
                secretEnc,
                subId,
            ) > 0
        }

    /** Set a subscription's status (e.g. resume -> 'active'). Returns true when a row was updated. */
    fun updateStatus(tenantId: UUID, subId: UUID, status: String): Boolean =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                "UPDATE auth.webhook_subscriptions SET status = ? WHERE sub_id = ?",
                status,
                subId,
            ) > 0
        }

    // ── Deliveries ─────────────────────────────────────────────────────────────────────────

    /** Enqueue a pending delivery for [subId]. `payloadJson` is the JSON body to sign + send. */
    fun insertDelivery(
        tenantId: UUID,
        subId: UUID,
        eventType: String,
        payloadJson: String,
        now: Instant = Instant.now(),
    ): UUID = tenantTx.inTenant(tenantId) { jdbc ->
        val id = UUID.randomUUID()
        jdbc.update(
            """
            INSERT INTO auth.webhook_deliveries
              (delivery_id, sub_id, tenant_id, event_type, payload, status, attempts, next_attempt_at)
            VALUES (?, ?, ?, ?, ?::jsonb, 'pending', 0, ?)
            """.trimIndent(),
            id,
            subId,
            tenantId,
            eventType,
            payloadJson,
            Timestamp.from(now),
        )
        id
    }

    /** List a subscription's deliveries, newest first, at most [limit]. */
    fun listDeliveries(tenantId: UUID, subId: UUID, limit: Int): List<Delivery> =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                SELECT delivery_id, sub_id, tenant_id, event_type, payload::text AS payload, status,
                       attempts, next_attempt_at, last_status_code, last_error, created_at, delivered_at
                  FROM auth.webhook_deliveries
                 WHERE sub_id = ?
                 ORDER BY created_at DESC
                 LIMIT ?
                """.trimIndent(),
                DELIVERY_MAPPER,
                subId,
                limit,
            )
        }

    /** Find one delivery by id within [tenantId]; null when absent (or RLS-invisible). */
    fun findDelivery(tenantId: UUID, deliveryId: UUID): Delivery? = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.query(
            """
            SELECT delivery_id, sub_id, tenant_id, event_type, payload::text AS payload, status,
                   attempts, next_attempt_at, last_status_code, last_error, created_at, delivered_at
              FROM auth.webhook_deliveries
             WHERE delivery_id = ?
            """.trimIndent(),
            DELIVERY_MAPPER,
            deliveryId,
        ).firstOrNull()
    }

    /**
     * Re-queue a past delivery as a brand-new pending row (replay) — the original is left untouched
     * as the historical record. The new row reuses the original's sub/event/payload. Returns the new
     * delivery id, or null if the source delivery does not exist for this tenant.
     */
    fun replayDelivery(tenantId: UUID, deliveryId: UUID, now: Instant = Instant.now()): UUID? {
        val source = findDelivery(tenantId, deliveryId) ?: return null
        return insertDelivery(tenantId, source.subId, source.eventType, source.payload, now)
    }

    /**
     * Tenant ids the worker fans out over to find due deliveries. `webhook_deliveries` is
     * RLS-scoped, so a platform read of it sees nothing — instead the worker reads due rows
     * tenant-by-tenant under each tenant's RLS context ([dueDeliveries]). This helper lists active
     * tenant ids from `auth.tenants` (platform-scoped, no RLS): a bounded superset of tenants that
     * could have due deliveries. (A tenant with no due rows simply yields an empty [dueDeliveries].)
     */
    fun activeTenantIds(): List<UUID> = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            """
            SELECT DISTINCT t.tenant_id
              FROM auth.tenants t
             WHERE t.status = 'active'
            """.trimIndent(),
            { rs, _ -> rs.getObject("tenant_id", UUID::class.java) },
        )
    }

    /**
     * Due deliveries for a single [tenantId], oldest-first, at most [limit]. Read inside the tenant's
     * RLS context. Due = `pending`, or `failed` with `next_attempt_at <= now`.
     */
    fun dueDeliveries(tenantId: UUID, now: Instant, limit: Int): List<Delivery> =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                SELECT delivery_id, sub_id, tenant_id, event_type, payload::text AS payload, status,
                       attempts, next_attempt_at, last_status_code, last_error, created_at, delivered_at
                  FROM auth.webhook_deliveries
                 WHERE status IN ('pending', 'failed')
                   AND next_attempt_at <= ?
                 ORDER BY next_attempt_at ASC
                 LIMIT ?
                """.trimIndent(),
                DELIVERY_MAPPER,
                Timestamp.from(now),
                limit,
            )
        }

    /** Mark a delivery delivered (2xx). Records the response status code and clears any error. */
    fun markDelivered(tenantId: UUID, deliveryId: UUID, attempts: Int, statusCode: Int, deliveredAt: Instant) {
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                UPDATE auth.webhook_deliveries
                   SET status = 'delivered', attempts = ?, last_status_code = ?, last_error = NULL,
                       delivered_at = ?
                 WHERE delivery_id = ?
                """.trimIndent(),
                attempts,
                statusCode,
                Timestamp.from(deliveredAt),
                deliveryId,
            )
        }
    }

    /**
     * Mark a delivery for retry (`failed`) with the next backoff window, OR terminal (`dead`) when
     * attempts are exhausted. The caller decides [terminal] and the [nextAttemptAt].
     */
    fun markFailed(
        tenantId: UUID,
        deliveryId: UUID,
        attempts: Int,
        statusCode: Int?,
        error: String?,
        nextAttemptAt: Instant,
        terminal: Boolean,
    ) {
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                UPDATE auth.webhook_deliveries
                   SET status = ?, attempts = ?, last_status_code = ?, last_error = ?, next_attempt_at = ?
                 WHERE delivery_id = ?
                """.trimIndent(),
                if (terminal) "dead" else "failed",
                attempts,
                statusCode,
                error?.take(MAX_ERROR_LENGTH),
                Timestamp.from(nextAttemptAt),
                deliveryId,
            )
        }
    }

    // ── Row mappers ──────────────────────────────────────────────────────────────────────

    private companion object {
        const val MAX_ERROR_LENGTH = 2000

        val SUBSCRIPTION_MAPPER = RowMapper { rs: ResultSet, _: Int -> mapSubscription(rs) }
        val DELIVERY_MAPPER = RowMapper { rs: ResultSet, _: Int -> mapDelivery(rs) }

        fun mapSubscription(rs: ResultSet): Subscription {
            @Suppress("UNCHECKED_CAST")
            val types = (rs.getArray("event_types")?.array as? Array<String>)?.toList() ?: emptyList()
            return Subscription(
                subId = rs.getObject("sub_id", UUID::class.java),
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                url = rs.getString("url"),
                eventTypes = types,
                secretEnc = rs.getBytes("secret_enc"),
                status = rs.getString("status"),
                createdAt = rs.getTimestamp("created_at").toInstant(),
            )
        }

        fun mapDelivery(rs: ResultSet): Delivery {
            val statusCode = rs.getInt("last_status_code").let { if (rs.wasNull()) null else it }
            return Delivery(
                deliveryId = rs.getObject("delivery_id", UUID::class.java),
                subId = rs.getObject("sub_id", UUID::class.java),
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                eventType = rs.getString("event_type"),
                payload = rs.getString("payload") ?: "{}",
                status = rs.getString("status"),
                attempts = rs.getInt("attempts"),
                nextAttemptAt = rs.getTimestamp("next_attempt_at").toInstant(),
                lastStatusCode = statusCode,
                lastError = rs.getString("last_error"),
                createdAt = rs.getTimestamp("created_at").toInstant(),
                deliveredAt = rs.getTimestamp("delivered_at")?.toInstant(),
            )
        }
    }
}
