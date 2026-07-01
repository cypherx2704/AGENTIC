package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.stereotype.Repository
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * JDBC access to `auth.audit_export_jobs` (Component 6 export audit trail — WP04).
 *
 * The table is TENANT-scoped (RLS `USING tenant_id = current_setting('app.tenant_id')::uuid`), so
 * every write goes through [TenantTx.inTenant] so the RLS predicate is satisfied. One row is
 * recorded per `GET /v1/audit-log/export` run for an operator audit trail (who exported what, where
 * it landed, when the presigned URL expires) — the URL itself is short-lived and is NOT persisted.
 */
@Repository
class AuditExportJobRepository(private val tenantTx: TenantTx) {

    /** Values for one export-job record. */
    data class NewExportJob(
        val tenantId: UUID,
        val requestedBy: UUID?,
        val storeBackend: String,
        val objectKey: String,
        val objectUri: String,
        val rowCount: Long,
        val truncated: Boolean,
        val windowFrom: Instant?,
        val windowTo: Instant?,
        val urlExpiresAt: Instant,
        val status: String,
    )

    /** Insert an export-job record, returning its generated `export_id`. Runs in the tenant tx (RLS). */
    fun record(job: NewExportJob): UUID = tenantTx.inTenant(job.tenantId) { jdbc ->
        jdbc.queryForObject(
            """
            INSERT INTO auth.audit_export_jobs
              (tenant_id, requested_by, store_backend, object_key, object_uri, row_count,
               truncated, window_from, window_to, url_expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING export_id
            """.trimIndent(),
            UUID::class.java,
            job.tenantId,
            job.requestedBy,
            job.storeBackend,
            job.objectKey,
            job.objectUri,
            job.rowCount,
            job.truncated,
            job.windowFrom?.let { Timestamp.from(it) },
            job.windowTo?.let { Timestamp.from(it) },
            Timestamp.from(job.urlExpiresAt),
            job.status,
        )!!
    }
}
