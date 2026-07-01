package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Repository
import java.math.BigDecimal
import java.sql.ResultSet
import java.util.UUID

/**
 * Platform-scoped access to `auth.rate_limit_config` (WP03 Component 4 — the self-protection
 * limits the [ai.cypherx.auth.web.RateLimitFilter] enforces).
 *
 * The table has NO RLS (only Auth reads/writes it), so every access goes through
 * [TenantTx.inPlatform]. The limiter loads EVERY row in ONE pass and caches the result in-memory,
 * refreshing on a cadence ([ai.cypherx.auth.config.RateLimitProperties.configRefreshSeconds]) — it
 * never hits the DB on the request hot path. A row with `tenant_id IS NULL` is the platform default
 * for its `(endpoint, scope_kind)`; a non-NULL `tenant_id` is an enterprise per-tenant override.
 */
@Repository
class RateLimitConfigRepository(
    private val tenantTx: TenantTx,
) {

    /** Every row of `auth.rate_limit_config`, loaded in a single platform transaction. */
    fun findAll(): List<RateLimitRule> =
        tenantTx.inPlatform { jdbc -> findAll(jdbc) }

    private fun findAll(jdbc: JdbcTemplate): List<RateLimitRule> =
        jdbc.query(
            "SELECT endpoint, scope_kind, tenant_id, limit_rpm, burst_multiplier, burst_seconds " +
                "FROM auth.rate_limit_config",
            { rs, _ -> mapRule(rs) },
        )

    private fun mapRule(rs: ResultSet): RateLimitRule =
        RateLimitRule(
            endpoint = rs.getString("endpoint"),
            scopeKind = rs.getString("scope_kind"),
            tenantId = rs.getObject("tenant_id", UUID::class.java),
            limitRpm = rs.getInt("limit_rpm"),
            burstMultiplier = rs.getBigDecimal("burst_multiplier") ?: BigDecimal.ONE,
            burstSeconds = rs.getInt("burst_seconds"),
        )
}

/**
 * One row of `auth.rate_limit_config`.
 *
 * @param endpoint        the endpoint pattern the rule guards (e.g. `/v1/agents/{id}/token`, or an
 *                        admin prefix with a trailing wildcard).
 * @param scopeKind       how the rate-limit key is derived: per-caller-service | per-tenant |
 *                        per-agent | per-service | per-admin-agent | per-ip.
 * @param tenantId        NULL = platform default; non-NULL = an enterprise per-tenant override.
 * @param limitRpm        sustained allowance, requests per minute.
 * @param burstMultiplier short-term burst multiplier applied for [burstSeconds] (effective ceiling
 *                        = ceil(limitRpm * burstMultiplier) when burst is honoured; 1.00 = no burst).
 * @param burstSeconds    how long (seconds) the burst allowance applies; 0 = no burst allowance.
 */
data class RateLimitRule(
    val endpoint: String,
    val scopeKind: String,
    val tenantId: UUID?,
    val limitRpm: Int,
    val burstMultiplier: BigDecimal,
    val burstSeconds: Int,
)
