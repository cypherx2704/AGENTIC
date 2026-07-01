package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.math.BigDecimal
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/** One hourly per-tenant usage bucket row (`auth.tenant_usage_counters`). */
data class TenantUsageCounter(
    val tenantId: UUID,
    val windowStart: Instant,
    val metric: String,
    val value: BigDecimal,
    val updatedAt: Instant,
)

/**
 * JDBC access to `auth.tenant_usage_counters` (Component 1d / Contract 19 — the `/v1/usage` rollup
 * target the WP04 `cypherx.llms.usage.recorded` consumer increments).
 *
 * The table is TENANT-scoped (RLS `USING tenant_id = current_setting('app.tenant_id')::uuid`), so
 * every access goes through [TenantTx.inTenant] so the predicate is satisfied for both the consumer's
 * upsert and the `/v1/usage` read. Buckets are hourly (UTC, truncated). The primary key
 * `(tenant_id, window_start, metric)` makes the increment an idempotent-per-key UPSERT.
 *
 * `/v1/usage` reads ONLY this rollup — there is NO cross-schema read into `llms.*` (Contract 19: the
 * usage document each service consumes is the auth-owned rollup, fed by the Kafka event).
 */
@Repository
class TenantUsageCounterRepository(private val tenantTx: TenantTx) {

    /**
     * Atomically add [delta] to the [tenantId]/[windowStart]/[metric] bucket, inserting the row when
     * it does not yet exist (the per-key UPSERT the usage consumer performs). [windowStart] MUST be
     * an hour-truncated UTC instant. Runs in a tenant tx so RLS admits the write.
     */
    fun increment(tenantId: UUID, windowStart: Instant, metric: String, delta: BigDecimal): Unit =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                INSERT INTO auth.tenant_usage_counters (tenant_id, window_start, metric, value, updated_at)
                VALUES (?, ?, ?, ?, NOW())
                ON CONFLICT (tenant_id, window_start, metric)
                DO UPDATE SET value = auth.tenant_usage_counters.value + EXCLUDED.value,
                              updated_at = NOW()
                """.trimIndent(),
                tenantId,
                Timestamp.from(windowStart),
                metric,
                delta,
            )
        }

    /**
     * Read the [tenantId]'s usage buckets in the window [from, to] (both optional). Rows are returned
     * ordered by `(window_start, metric)`. RLS confines the read to the tenant. The `/v1/usage`
     * endpoint aggregates these into per-metric totals + the hourly series.
     */
    fun read(tenantId: UUID, from: Instant?, to: Instant?): List<TenantUsageCounter> =
        tenantTx.inTenant(tenantId) { jdbc ->
            val sql = StringBuilder(
                "SELECT tenant_id, window_start, metric, value, updated_at " +
                    "FROM auth.tenant_usage_counters WHERE tenant_id = ?",
            )
            val args = mutableListOf<Any?>(tenantId)
            from?.let { sql.append(" AND window_start >= ?"); args.add(Timestamp.from(it)) }
            to?.let { sql.append(" AND window_start < ?"); args.add(Timestamp.from(it)) }
            sql.append(" ORDER BY window_start ASC, metric ASC")
            jdbc.query(sql.toString(), ROW_MAPPER, *args.toTypedArray())
        }

    private companion object {
        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            TenantUsageCounter(
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                windowStart = rs.getTimestamp("window_start").toInstant(),
                metric = rs.getString("metric"),
                value = rs.getBigDecimal("value") ?: BigDecimal.ZERO,
                updatedAt = rs.getTimestamp("updated_at").toInstant(),
            )
        }
    }
}
