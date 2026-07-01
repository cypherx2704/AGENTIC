package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.time.Instant
import java.util.UUID

/**
 * The in-memory projection of an `auth.tenant_quotas` row. `limitsJson` is the stored JSONB
 * `limits` document surfaced as its raw JSON string (the service layer parses + deep-merges).
 *
 * `tenant_quotas` is an append-only, effective-dated history: the CURRENT row is the one with
 * `effective_until IS NULL`. A new override "closes" the previous current row (sets its
 * `effective_until = NOW()`) and inserts a fresh row with `effective_from = NOW()`,
 * `effective_until = NULL`.
 */
data class TenantQuotaRow(
    val tenantId: UUID,
    val plan: String,
    val limitsJson: String,
    val effectiveFrom: Instant,
    val effectiveUntil: Instant?,
    val source: String,
    val updatedBy: String,
)

/**
 * JDBC access to `auth.tenant_quotas` (Component 1d / Contract 19 — per-tenant effective quota
 * overrides).
 *
 * `tenant_quotas` is TENANT-scoped: it carries RLS (`USING tenant_id = current_setting(
 * 'app.tenant_id')::uuid`), so every access goes through [TenantTx.inTenant] so the predicate is
 * satisfied for both reads and writes.
 *
 * History model (append-only, effective-dated): there is at most ONE current row per tenant
 * (`effective_until IS NULL`, enforced by the partial index `ix_tenant_quotas_current`). Setting a
 * new override is a two-step write in ONE transaction: close the current row, then insert the new
 * current row. The whole history is retained for audit; nothing is updated in place except the
 * `effective_until` close-stamp.
 */
@Repository
class TenantQuotaRepository(private val tenantTx: TenantTx) {

    /** Fetch the CURRENT quota row for [tenantId] (`effective_until IS NULL`), or null if none. */
    fun findCurrent(tenantId: UUID): TenantQuotaRow? = tenantTx.inTenant(tenantId) { jdbc ->
        readCurrent(jdbc, tenantId)
    }

    /**
     * Same as [findCurrent] but reuses an already-open tenant transaction's [JdbcTemplate] (so a
     * caller that also writes the new override + an outbox row stays on ONE tenant tx).
     */
    fun readCurrent(jdbc: JdbcTemplate, tenantId: UUID): TenantQuotaRow? =
        jdbc.query(SELECT_CURRENT, ROW_MAPPER, tenantId).firstOrNull()

    /**
     * Append a new current quota row for [tenantId], closing the previous current row in the SAME
     * transaction (append-only effective-dated history). The new row's `limits` is [limitsJson]
     * (the already-merged effective override document), `plan` is the tenant's current plan,
     * `source` is one of `plan-default` | `admin-override` | `billing-event`, and `updated_by`
     * records the actor.
     *
     * Runs inside [TenantTx.inTenant] so RLS admits both the UPDATE (close) and INSERT (open). The
     * returned row is the freshly-inserted current row.
     */
    fun appendOverride(
        tenantId: UUID,
        plan: String,
        limitsJson: String,
        source: String,
        updatedBy: String,
    ): TenantQuotaRow = tenantTx.inTenant(tenantId) { jdbc ->
        appendOverrideInTx(jdbc, tenantId, plan, limitsJson, source, updatedBy)
    }

    /**
     * Same as [appendOverride] but reuses an already-open tenant transaction's [JdbcTemplate], so
     * the close+insert AND a transactional-outbox row written by the caller all commit atomically.
     * [jdbc] MUST be the template the surrounding [TenantTx.inTenant] block handed out.
     */
    fun appendOverrideInTx(
        jdbc: JdbcTemplate,
        tenantId: UUID,
        plan: String,
        limitsJson: String,
        source: String,
        updatedBy: String,
    ): TenantQuotaRow {
        // 1) Close the previous current row (no-op when the tenant has no current row yet).
        jdbc.update(
            """
            UPDATE auth.tenant_quotas
               SET effective_until = NOW()
             WHERE tenant_id = ? AND effective_until IS NULL
            """.trimIndent(),
            tenantId,
        )
        // 2) Insert the new current row (effective_from = NOW(), effective_until = NULL).
        jdbc.update(
            """
            INSERT INTO auth.tenant_quotas
              (tenant_id, plan, limits, effective_from, effective_until, source, updated_by)
            VALUES (?, ?, ?::jsonb, NOW(), NULL, ?, ?)
            """.trimIndent(),
            tenantId,
            plan,
            limitsJson,
            source,
            updatedBy,
        )
        return readCurrent(jdbc, tenantId)
            ?: error("tenant_quotas current row missing immediately after insert for tenant $tenantId")
    }

    private companion object {
        const val SELECT_CURRENT =
            """
            SELECT tenant_id, plan, limits::text AS limits, effective_from, effective_until,
                   source, updated_by
              FROM auth.tenant_quotas
             WHERE tenant_id = ? AND effective_until IS NULL
            """

        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            TenantQuotaRow(
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                plan = rs.getString("plan"),
                limitsJson = rs.getString("limits") ?: "{}",
                effectiveFrom = rs.getTimestamp("effective_from").toInstant(),
                effectiveUntil = rs.getTimestamp("effective_until")?.toInstant(),
                source = rs.getString("source"),
                updatedBy = rs.getString("updated_by"),
            )
        }
    }
}
