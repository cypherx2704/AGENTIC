package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.ConnectionCallback
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/** A HIL approval request row (subset of auth.approval_requests + operation context). */
data class ApprovalRequestRow(
    val requestId: UUID,
    val tenantId: UUID,
    val agentId: UUID,
    val operationType: String?,
    val operationContextJson: String,
    val status: String,
    val requestedAt: Instant,
    val expiresAt: Instant,
    val resolvedAt: Instant?,
    val resolutionNote: String?,
)

/** A tenant orchestrator's HIL mode config. */
data class HilConfigRow(
    val agentId: UUID,
    val defaultMode: String,
    val askOnTriggers: List<String>,
)

/**
 * Tenant-scoped persistence for the HIL framework (Phase 6): operation-approval requests on
 * `auth.approval_requests` and the per-orchestrator `auth.orchestrator_hil_config`.
 */
@Repository
class HilRepository(private val tenantTx: TenantTx) {

    private val rowMapper = RowMapper { rs: ResultSet, _: Int -> mapRow(rs) }

    fun insertRequest(
        tenantId: UUID,
        agentId: UUID,
        operationType: String,
        operationContextJson: String,
        expiresAt: Instant,
    ): ApprovalRequestRow = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.queryForObject(
            """
            INSERT INTO auth.approval_requests
                (tenant_id, agent_id, operation_type, operation_context, status, expires_at)
            VALUES (?, ?, ?, ?::jsonb, 'pending', ?)
            RETURNING $COLS
            """.trimIndent(),
            rowMapper,
            tenantId,
            agentId,
            operationType,
            operationContextJson,
            Timestamp.from(expiresAt),
        ) ?: error("INSERT ... RETURNING produced no approval_request row")
    }

    fun getRequest(tenantId: UUID, requestId: UUID): ApprovalRequestRow? =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query("SELECT $COLS FROM auth.approval_requests WHERE request_id = ?", rowMapper, requestId)
                .firstOrNull()
        }

    fun listPending(tenantId: UUID, operationType: String?): List<ApprovalRequestRow> =
        tenantTx.inTenant(tenantId) { jdbc ->
            val sql = StringBuilder(
                "SELECT $COLS FROM auth.approval_requests WHERE status = 'pending'",
            )
            val args = mutableListOf<Any?>()
            operationType?.let { sql.append(" AND operation_type = ?"); args.add(it) }
            sql.append(" ORDER BY requested_at DESC LIMIT 200")
            jdbc.query(sql.toString(), rowMapper, *args.toTypedArray())
        }

    /** Resolve a pending request to `granted`/`denied`. Returns true iff a pending row was updated. */
    fun resolve(tenantId: UUID, requestId: UUID, decision: String, resolvedBy: UUID, note: String?): Boolean =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.update(
                """
                UPDATE auth.approval_requests
                   SET status = ?, resolved_at = NOW(), resolved_by = ?, resolution_note = ?
                 WHERE request_id = ? AND status = 'pending'
                """.trimIndent(),
                decision,
                resolvedBy,
                note,
                requestId,
            ) == 1
        }

    fun getHilConfig(tenantId: UUID, agentId: UUID): HilConfigRow? = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.query(
            "SELECT agent_id, default_mode, ask_on_triggers FROM auth.orchestrator_hil_config WHERE agent_id = ?",
            { rs: ResultSet, _: Int ->
                @Suppress("UNCHECKED_CAST")
                val triggers = (rs.getArray("ask_on_triggers")?.array as? Array<String>)?.toList() ?: emptyList()
                HilConfigRow(
                    agentId = rs.getObject("agent_id", UUID::class.java),
                    defaultMode = rs.getString("default_mode"),
                    askOnTriggers = triggers,
                )
            },
            agentId,
        ).firstOrNull()
    }

    fun upsertHilConfig(tenantId: UUID, agentId: UUID, defaultMode: String, askOnTriggers: List<String>): HilConfigRow =
        tenantTx.inTenant(tenantId) { jdbc ->
            val triggersArray = jdbc.execute(
                ConnectionCallback { con -> con.createArrayOf("text", askOnTriggers.toTypedArray()) },
            )
            jdbc.update(
                """
                INSERT INTO auth.orchestrator_hil_config (agent_id, tenant_id, default_mode, ask_on_triggers, updated_at)
                VALUES (?, ?, ?, ?, NOW())
                ON CONFLICT (agent_id) DO UPDATE
                   SET default_mode = EXCLUDED.default_mode,
                       ask_on_triggers = EXCLUDED.ask_on_triggers,
                       updated_at = NOW()
                """.trimIndent(),
                agentId,
                tenantId,
                defaultMode,
                triggersArray,
            )
            HilConfigRow(agentId, defaultMode, askOnTriggers)
        }

    private fun mapRow(rs: ResultSet): ApprovalRequestRow = ApprovalRequestRow(
        requestId = rs.getObject("request_id", UUID::class.java),
        tenantId = rs.getObject("tenant_id", UUID::class.java),
        agentId = rs.getObject("agent_id", UUID::class.java),
        operationType = rs.getString("operation_type"),
        operationContextJson = rs.getString("operation_context") ?: "{}",
        status = rs.getString("status"),
        requestedAt = rs.getTimestamp("requested_at").toInstant(),
        expiresAt = rs.getTimestamp("expires_at").toInstant(),
        resolvedAt = rs.getTimestamp("resolved_at")?.toInstant(),
        resolutionNote = rs.getString("resolution_note"),
    )

    private companion object {
        const val COLS =
            "request_id, tenant_id, agent_id, operation_type, " +
                "operation_context::text AS operation_context, status, requested_at, expires_at, " +
                "resolved_at, resolution_note"
    }
}
