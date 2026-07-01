package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.TenantSource
import ai.cypherx.auth.domain.TenantStatus
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * The in-memory projection of an `auth.tenants` row. `sourceMetadata` / quota `limits` are stored
 * as JSONB in PostgreSQL and surfaced here as their raw JSON string (callers parse as needed).
 */
data class Tenant(
    val tenantId: UUID,
    val name: String,
    val status: TenantStatus,
    val plan: String,
    val source: TenantSource,
    val sourceMetadataJson: String,
    val region: String,
    val createdAt: Instant,
    val updatedAt: Instant,
    val suspendedAt: Instant?,
    val pendingDeletionAt: Instant?,
    val deletedAt: Instant?,
)

/**
 * JDBC access to `auth.tenants`, `auth.plan_defaults`, and `auth.tenant_quotas`.
 *
 * `auth.tenants` is PLATFORM-scoped (no RLS) per Contract 13 — every access goes through
 * [TenantTx.inPlatform] (a plain transaction with NO `app.tenant_id` set). `tenant_quotas` is
 * tenant-scoped by RLS, but quota SEEDING happens at tenant-creation time inside the platform tx
 * with an explicit `tenant_id` literal we control, so we keep all tenant-admin writes on one
 * code path here.
 *
 * NOTE: `tenant_quotas` carries RLS (`USING tenant_id = current_setting('app.tenant_id')`). Seeding
 * a brand-new tenant's quota row therefore runs in [TenantTx.inTenant] so the RLS predicate is
 * satisfied for the INSERT.
 */
@Repository
class TenantRepository(private val tenantTx: TenantTx) {

    /**
     * Insert a new tenant row. The caller supplies the [tenantId] (matches px0.org_id when bridged,
     * else a freshly generated UUID). Throws [DuplicateKeyException] if the id already exists so the
     * service can map it to a Contract 2 CONFLICT.
     */
    fun insert(
        tenantId: UUID,
        name: String,
        plan: String,
        source: TenantSource,
        sourceMetadataJson: String,
        region: String,
    ): Tenant = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            INSERT INTO auth.tenants
              (tenant_id, name, status, plan, source, source_metadata, region)
            VALUES (?, ?, 'active', ?, ?, ?::jsonb, ?)
            """.trimIndent(),
            tenantId,
            name,
            plan,
            source.value,
            sourceMetadataJson,
            region,
        )
        jdbc.queryForObject(SELECT_BY_ID, ROW_MAPPER, tenantId)!!
    }

    /** Fetch a single tenant by id, or null when absent. */
    fun findById(tenantId: UUID): Tenant? = tenantTx.inPlatform { jdbc ->
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, tenantId).firstOrNull()
    }

    /**
     * Cursor-paginated list (Contract 9). The cursor is the `created_at` (epoch-millis) + `tenant_id`
     * of the last row of the previous page — a stable, opaque keyset. We fetch [limit] + 1 rows so the
     * caller can compute `has_more`. [includeDeleted] controls whether soft-deleted tenants surface
     * (admin list defaults to hiding `deleted`).
     */
    fun list(
        limit: Int,
        afterCreatedAt: Instant?,
        afterTenantId: UUID?,
        includeDeleted: Boolean,
    ): List<Tenant> = tenantTx.inPlatform { jdbc ->
        val sql = StringBuilder("SELECT * FROM auth.tenants WHERE 1=1")
        val args = mutableListOf<Any>()
        if (!includeDeleted) {
            sql.append(" AND status <> 'deleted'")
        }
        if (afterCreatedAt != null && afterTenantId != null) {
            // Keyset: rows strictly "after" the cursor under (created_at DESC, tenant_id DESC).
            sql.append(" AND (created_at, tenant_id) < (?, ?)")
            args.add(Timestamp.from(afterCreatedAt))
            args.add(afterTenantId)
        }
        sql.append(" ORDER BY created_at DESC, tenant_id DESC LIMIT ?")
        args.add(limit)
        jdbc.query(sql.toString(), ROW_MAPPER, *args.toTypedArray())
    }

    /**
     * Update mutable, caller-editable tenant fields (`name`, `region`, `source_metadata`). Only
     * non-null arguments are applied. Returns the refreshed row, or null if the tenant is gone.
     */
    fun updateMutable(
        tenantId: UUID,
        name: String?,
        region: String?,
        sourceMetadataJson: String?,
    ): Tenant? = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            UPDATE auth.tenants
               SET name            = COALESCE(?, name),
                   region          = COALESCE(?, region),
                   source_metadata = COALESCE(?::jsonb, source_metadata),
                   updated_at      = NOW()
             WHERE tenant_id = ? AND status <> 'deleted'
            """.trimIndent(),
            name,
            region,
            sourceMetadataJson,
            tenantId,
        )
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, tenantId).firstOrNull()
    }

    /** Change a tenant's plan (used by suspend/resume callers' siblings and plan-change flows). */
    fun updatePlan(tenantId: UUID, newPlan: String): Tenant? = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.tenants SET plan = ?, updated_at = NOW() WHERE tenant_id = ? AND status <> 'deleted'",
            newPlan,
            tenantId,
        )
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, tenantId).firstOrNull()
    }

    /** Move a tenant to `suspended`, stamping `suspended_at`. Returns the refreshed row. */
    fun suspend(tenantId: UUID): Tenant? = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            UPDATE auth.tenants
               SET status = 'suspended', suspended_at = NOW(), updated_at = NOW()
             WHERE tenant_id = ? AND status <> 'deleted'
            """.trimIndent(),
            tenantId,
        )
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, tenantId).firstOrNull()
    }

    /** Move a tenant back to `active`, clearing `suspended_at`. Returns the refreshed row. */
    fun resume(tenantId: UUID): Tenant? = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            UPDATE auth.tenants
               SET status = 'active', suspended_at = NULL, updated_at = NOW()
             WHERE tenant_id = ? AND status <> 'deleted'
            """.trimIndent(),
            tenantId,
        )
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, tenantId).firstOrNull()
    }

    /**
     * Soft-delete: move a tenant to `pending_deletion` and stamp `pending_deletion_at`. The row is
     * retained for the 30-day grace window (a later hard-delete job flips it to `deleted` and stamps
     * `deleted_at`). Returns the refreshed row.
     */
    fun softDelete(tenantId: UUID): Tenant? = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            UPDATE auth.tenants
               SET status = 'pending_deletion', pending_deletion_at = NOW(), updated_at = NOW()
             WHERE tenant_id = ? AND status <> 'deleted'
            """.trimIndent(),
            tenantId,
        )
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, tenantId).firstOrNull()
    }

    /** Read the default quota `limits` JSON for [plan] from `auth.plan_defaults`, or null if unknown. */
    fun planDefaultLimits(plan: String): String? = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT limits::text AS limits FROM auth.plan_defaults WHERE plan = ?",
            { rs, _ -> rs.getString("limits") },
            plan,
        ).firstOrNull()
    }

    /**
     * Seed the initial `tenant_quotas` row for a freshly-created tenant from its plan defaults
     * (`source = 'plan-default'`). Runs inside an [TenantTx.inTenant] tx because `tenant_quotas`
     * carries RLS keyed on `app.tenant_id`. Idempotent: a duplicate `(tenant_id, effective_from)`
     * (extremely unlikely given `NOW()`) is swallowed.
     */
    fun seedQuotasFromPlan(
        tenantId: UUID,
        plan: String,
        limitsJson: String,
        updatedBy: String,
    ): Unit = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.update(
            """
            INSERT INTO auth.tenant_quotas
              (tenant_id, plan, limits, effective_from, effective_until, source, updated_by)
            VALUES (?, ?, ?::jsonb, NOW(), NULL, 'plan-default', ?)
            ON CONFLICT (tenant_id, effective_from) DO NOTHING
            """.trimIndent(),
            tenantId,
            plan,
            limitsJson,
            updatedBy,
        )
    }

    private companion object {
        const val SELECT_BY_ID =
            "SELECT * FROM auth.tenants WHERE tenant_id = ?"

        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            Tenant(
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                name = rs.getString("name"),
                status = TenantStatus.from(rs.getString("status")),
                plan = rs.getString("plan"),
                source = TenantSource.from(rs.getString("source")),
                sourceMetadataJson = rs.getString("source_metadata") ?: "{}",
                region = rs.getString("region"),
                createdAt = rs.getTimestamp("created_at").toInstant(),
                updatedAt = rs.getTimestamp("updated_at").toInstant(),
                suspendedAt = rs.getTimestamp("suspended_at")?.toInstant(),
                pendingDeletionAt = rs.getTimestamp("pending_deletion_at")?.toInstant(),
                deletedAt = rs.getTimestamp("deleted_at")?.toInstant(),
            )
        }
    }
}
