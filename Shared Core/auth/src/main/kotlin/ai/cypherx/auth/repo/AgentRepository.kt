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
 * Immutable view of an `auth.agents` row (Component 1, Phase 2). Only the columns feature code in
 * this phase needs are projected; the table also has description/allowed_tools/allowed_skills/
 * quarantine_until, surfaced when those features land.
 *
 * Orchestrator-hierarchy columns (WP-orchestrator): [agentType], [parentOrchestratorId],
 * [immutableLlm], [ownerUserId]. See [ai.cypherx.auth.domain.AgentType].
 */
data class AgentRecord(
    val agentId: UUID,
    val tenantId: UUID,
    val name: String,
    val version: String,
    val status: String,
    val allowedScopes: List<String>,
    val capabilities: String,
    val metadata: String,
    val createdBy: UUID,
    val createdAt: Instant,
    val updatedAt: Instant,
    val agentType: String,
    val parentOrchestratorId: UUID?,
    val immutableLlm: Boolean,
    val ownerUserId: UUID?,
)

/**
 * Tenant-scoped persistence for `auth.agents` (Contract 13: every access goes through
 * [TenantTx.inTenant], which sets `app.tenant_id` so PostgreSQL RLS confines reads/writes to the
 * caller's tenant). NO JPA — NamedParameter-free JdbcTemplate on the tx-bound connection.
 *
 * Note: `auth.agents` has RLS `USING (tenant_id = app.tenant_id)`; with no separate `WITH CHECK`,
 * PostgreSQL reuses that predicate for INSERT, so an insert whose `tenant_id` differs from the bound
 * tenant is rejected — defence in depth on top of the application-level tenant resolution.
 */
@Repository
class AgentRepository(
    private val tenantTx: TenantTx,
) {

    private val rowMapper = RowMapper { rs: ResultSet, _: Int -> mapRow(rs) }

    /**
     * Insert a new agent and return the persisted row. Uniqueness is enforced by
     * `agents_tenant_name_version_unique (tenant_id, name, version)`; the caller maps the resulting
     * `DuplicateKeyException` to a Contract 2 409. A second orchestrator in the same tenant is
     * rejected by `uq_orchestrator_per_tenant` (also a DuplicateKeyException).
     *
     * `capabilities`/`metadata` are JSONB — passed as text and cast `::jsonb` in SQL.
     * `allowed_scopes` is `TEXT[]` — passed as a real `java.sql.Array`.
     */
    fun insert(
        tenantId: UUID,
        name: String,
        version: String,
        allowedScopes: List<String>,
        capabilities: String,
        metadata: String,
        createdBy: UUID,
        agentType: String = "user_created",
        parentOrchestratorId: UUID? = null,
        immutableLlm: Boolean = false,
        ownerUserId: UUID? = null,
    ): AgentRecord = tenantTx.inTenant(tenantId) { jdbc ->
        val scopesArray = jdbc.execute(
            ConnectionCallback { con -> con.createArrayOf("text", allowedScopes.toTypedArray()) },
        )
        jdbc.queryForObject(
            """
            INSERT INTO auth.agents
                (tenant_id, name, version, allowed_scopes, capabilities, metadata, created_by,
                 agent_type, parent_orchestrator_id, immutable_llm, owner_user_id)
            VALUES (?, ?, ?, ?, ?::jsonb, ?::jsonb, ?, ?, ?, ?, ?)
            RETURNING $COLS
            """.trimIndent(),
            rowMapper,
            tenantId,
            name,
            version,
            scopesArray,
            capabilities,
            metadata,
            createdBy,
            agentType,
            parentOrchestratorId,
            immutableLlm,
            ownerUserId,
        ) ?: error("INSERT ... RETURNING produced no row for agent $name")
    }

    /** Find one agent by id within [tenantId]. Returns null when absent (or RLS-invisible). */
    fun findById(tenantId: UUID, agentId: UUID): AgentRecord? = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.query(
            "SELECT $COLS FROM auth.agents WHERE agent_id = ?",
            rowMapper,
            agentId,
        ).firstOrNull()
    }

    /** The tenant's single orchestrator agent (or null if none exists yet). */
    fun findOrchestrator(tenantId: UUID): AgentRecord? = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.query(
            "SELECT $COLS FROM auth.agents WHERE agent_type = 'orchestrator' LIMIT 1",
            rowMapper,
        ).firstOrNull()
    }

    /** Count agents in [tenantId] (RLS-scoped). Backs the Contract-19 `auth.agents_max` quota. */
    fun countByTenant(tenantId: UUID): Long = tenantTx.inTenant(tenantId) { jdbc ->
        jdbc.queryForObject("SELECT COUNT(*) FROM auth.agents", Long::class.java) ?: 0L
    }

    /**
     * Keyset-paginated list of [tenantId]'s agents (RLS-scoped), newest first. The cursor is the
     * `(created_at, agent_id)` of the last row of the previous page — a composite keyset avoids the
     * skip/duplicate hazard a non-unique `created_at`-only cursor has. Optional [statusFilter] (exact
     * match) and [nameContains] (case-insensitive substring) narrow the result. When
     * [parentOrchestratorId] is non-null, only sub-agents of that orchestrator are returned (used by
     * the orchestrator's own sub-agent listing). [limit] is clamped by the caller.
     */
    fun list(
        tenantId: UUID,
        statusFilter: String?,
        nameContains: String?,
        afterCreatedAt: Instant?,
        afterAgentId: UUID?,
        limit: Int,
        parentOrchestratorId: UUID? = null,
    ): List<AgentRecord> = tenantTx.inTenant(tenantId) { jdbc ->
        val sql = StringBuilder("SELECT $COLS FROM auth.agents WHERE 1 = 1")
        val args = mutableListOf<Any?>()
        statusFilter?.let { sql.append(" AND status = ?"); args.add(it) }
        nameContains?.let { sql.append(" AND name ILIKE ?"); args.add("%${escapeLike(it)}%") }
        parentOrchestratorId?.let { sql.append(" AND parent_orchestrator_id = ?"); args.add(it) }
        // Keyset: rows strictly "older" than the cursor under (created_at DESC, agent_id DESC).
        if (afterCreatedAt != null && afterAgentId != null) {
            sql.append(" AND (created_at, agent_id) < (?, ?)")
            args.add(Timestamp.from(afterCreatedAt))
            args.add(afterAgentId)
        }
        sql.append(" ORDER BY created_at DESC, agent_id DESC LIMIT ?")
        args.add(limit)
        jdbc.query(sql.toString(), rowMapper, *args.toTypedArray())
    }

    /**
     * Partially update an agent's mutable fields within [tenantId]. Only the non-null arguments are
     * written ([allowedScopes] as `TEXT[]`, [capabilities]/[metadata] as JSONB text cast `::jsonb`);
     * `updated_at` is always bumped. Returns the refreshed row, or null when the agent is absent
     * (or RLS-invisible). A no-op call (all nulls) still bumps `updated_at` and returns the row.
     */
    fun updatePartial(
        tenantId: UUID,
        agentId: UUID,
        allowedScopes: List<String>?,
        capabilities: String?,
        metadata: String?,
    ): AgentRecord? = tenantTx.inTenant(tenantId) { jdbc ->
        val sets = mutableListOf<String>()
        val args = mutableListOf<Any?>()
        if (allowedScopes != null) {
            val scopesArray = jdbc.execute(
                ConnectionCallback { con -> con.createArrayOf("text", allowedScopes.toTypedArray()) },
            )
            sets.add("allowed_scopes = ?")
            args.add(scopesArray)
        }
        if (capabilities != null) {
            sets.add("capabilities = ?::jsonb")
            args.add(capabilities)
        }
        if (metadata != null) {
            sets.add("metadata = ?::jsonb")
            args.add(metadata)
        }
        sets.add("updated_at = NOW()")
        args.add(agentId)
        jdbc.query(
            """
            UPDATE auth.agents
               SET ${sets.joinToString(", ")}
             WHERE agent_id = ?
            RETURNING $COLS
            """.trimIndent(),
            rowMapper,
            *args.toTypedArray(),
        ).firstOrNull()
    }

    /**
     * Set an agent's [status] within [tenantId], bumping `updated_at`. Returns the refreshed row, or
     * null when the agent is absent (or RLS-invisible). Used by the deactivate cascade; idempotent at
     * the row level (re-setting the same status is a harmless update).
     */
    fun updateStatus(tenantId: UUID, agentId: UUID, status: String): AgentRecord? =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                UPDATE auth.agents
                   SET status = ?, updated_at = NOW()
                 WHERE agent_id = ?
                RETURNING $COLS
                """.trimIndent(),
                rowMapper,
                status,
                agentId,
            ).firstOrNull()
        }

    /** Escape LIKE/ILIKE wildcards in a user-supplied substring so `%`/`_` match literally. */
    private fun escapeLike(value: String): String =
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    private fun mapRow(rs: ResultSet): AgentRecord {
        @Suppress("UNCHECKED_CAST")
        val scopes = (rs.getArray("allowed_scopes")?.array as? Array<String>)?.toList() ?: emptyList()
        return AgentRecord(
            agentId = rs.getObject("agent_id", UUID::class.java),
            tenantId = rs.getObject("tenant_id", UUID::class.java),
            name = rs.getString("name"),
            version = rs.getString("version"),
            status = rs.getString("status"),
            allowedScopes = scopes,
            capabilities = rs.getString("capabilities") ?: "[]",
            metadata = rs.getString("metadata") ?: "{}",
            createdBy = rs.getObject("created_by", UUID::class.java),
            createdAt = rs.getTimestamp("created_at").toInstant(),
            updatedAt = rs.getTimestamp("updated_at").toInstant(),
            agentType = rs.getString("agent_type") ?: "user_created",
            parentOrchestratorId = rs.getObject("parent_orchestrator_id", UUID::class.java),
            immutableLlm = rs.getBoolean("immutable_llm"),
            ownerUserId = rs.getObject("owner_user_id", UUID::class.java),
        )
    }

    private companion object {
        /** Canonical projection — every SELECT/RETURNING uses this so the column set stays in sync. */
        const val COLS =
            "agent_id, tenant_id, name, version, status, allowed_scopes, " +
                "capabilities::text AS capabilities, metadata::text AS metadata, " +
                "created_by, created_at, updated_at, " +
                "agent_type, parent_orchestrator_id, immutable_llm, owner_user_id"
    }
}
