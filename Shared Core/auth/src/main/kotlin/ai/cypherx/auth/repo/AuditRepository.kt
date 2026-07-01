package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * Append-only persistence for `auth.audit_log` (Component 6, Phase 2).
 *
 * The table is tenant-scoped (RLS) and INSERT/SELECT only for the runtime role — there is NO
 * UPDATE/DELETE path here by design (tamper-evidence). Every write extends a per-tenant SHA-256
 * hash chain:
 *
 *     row_hash(N)      = sha256( canonical_payload(N) || prev_row_hash(N) )
 *     prev_row_hash(N) = row_hash(N-1) for the same tenant   (genesis: 32 zero bytes)
 *
 * The chain tip is read inside the SAME transaction as the insert (`... ORDER BY id DESC LIMIT 1
 * FOR UPDATE`) so concurrent appends for a tenant serialise on the tip row and cannot fork the
 * chain. (The Valkey `audit-chain-tip:{tenant}` fast-path from the phase doc is an optimisation
 * layered on top later; correctness lives in this DB read.)
 *
 * Hashing is delegated to [ai.cypherx.auth.service.AuditService] (it owns the digest format); this
 * repository only persists the prepared row and exposes reads for the verify endpoint.
 */
@Repository
class AuditRepository(
    private val tenantTx: TenantTx,
) {

    /** A persisted audit row (read side). */
    data class AuditRow(
        val id: Long,
        val eventType: String,
        val agentId: UUID?,
        val tenantId: UUID,
        val action: String?,
        val resource: String?,
        val decision: String?,
        val policyIds: List<String>,
        val requestId: UUID?,
        val traceId: UUID?,
        val ipAddress: String?,
        val createdAt: Instant,
        val rowHash: ByteArray,
        val prevRowHash: ByteArray,
    )

    /** The values needed to insert a row; [AuditRow.rowHash]/prev are computed by AuditService. */
    data class NewAuditRow(
        val eventType: String,
        val agentId: UUID?,
        val tenantId: UUID,
        val action: String?,
        val resource: String?,
        val decision: String?,
        val policyIds: List<String>,
        val requestId: UUID?,
        val traceId: UUID?,
        val ipAddress: String?,
        val createdAt: Instant,
    )

    /** Result of a successful append: the new row id and the hashes that now form the tip. */
    data class Appended(val id: Long, val rowHash: ByteArray, val prevRowHash: ByteArray)

    /** 32 zero bytes — the genesis `prev_row_hash` for the first row of a tenant chain. */
    val genesisHash: ByteArray get() = ByteArray(32)

    /**
     * Append one audit row atomically: open a tenant tx, lock+read the tip, hand it to [hash] to
     * compute the row_hash, insert, and return the new row id + its row_hash (the new tip). [hash]
     * receives the prev hash and the row, and returns the computed row_hash.
     */
    fun appendInTenant(
        tenantId: UUID,
        row: NewAuditRow,
        hash: (prevRowHash: ByteArray, row: NewAuditRow) -> ByteArray,
    ): Appended =
        tenantTx.inTenant(tenantId) { jdbc ->
            val prev = currentTip(jdbc, tenantId) ?: genesisHash
            val rowHash = hash(prev, row)
            val id = insert(jdbc, row, prev, rowHash)
            Appended(id = id, rowHash = rowHash, prevRowHash = prev)
        }

    /**
     * Read the current chain tip (latest `row_hash`) for [tenantId]. Concurrent appends for the same
     * tenant are serialized by a per-tenant TRANSACTION-scoped advisory lock rather than `SELECT ...
     * FOR UPDATE`: audit_log is append-only and the runtime role intentionally lacks UPDATE (for
     * tamper-evidence), but FOR UPDATE requires the UPDATE privilege ("permission denied for table
     * audit_log"). The advisory lock gives the same serialization with only SELECT/INSERT rights and
     * releases automatically at commit/rollback. null when the tenant has no rows yet.
     */
    private fun currentTip(jdbc: JdbcTemplate, tenantId: UUID): ByteArray? {
        // hashtextextended(text, seed) -> bigint; pg_advisory_xact_lock(bigint) needs no table grant.
        jdbc.query(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            { _, _ -> 0 },
            "audit:$tenantId",
        )
        return jdbc.query(
            "SELECT row_hash FROM auth.audit_log WHERE tenant_id = ? ORDER BY id DESC LIMIT 1",
            { rs, _ -> rs.getBytes("row_hash") },
            tenantId,
        ).firstOrNull()
    }

    /**
     * Insert the row and return its generated id. `policy_ids` is passed as a PostgreSQL array
     * literal cast with `?::text[]` (NULL when empty) — avoids a second connection checkout for
     * `createArrayOf`. Runs on the passed-in [jdbc] (same tenant tx as the tip read).
     */
    private fun insert(jdbc: JdbcTemplate, row: NewAuditRow, prevRowHash: ByteArray, rowHash: ByteArray): Long =
        jdbc.queryForObject(
            """
            INSERT INTO auth.audit_log
              (event_type, agent_id, tenant_id, action, resource, decision, policy_ids,
               request_id, trace_id, ip_address, created_at, row_hash, prev_row_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?::text[], ?, ?, ?::inet, ?, ?, ?)
            RETURNING id
            """.trimIndent(),
            Long::class.java,
            row.eventType,
            row.agentId,
            row.tenantId,
            row.action,
            row.resource,
            row.decision,
            toPgTextArray(row.policyIds),
            row.requestId,
            row.traceId,
            row.ipAddress,
            Timestamp.from(row.createdAt),
            rowHash,
            prevRowHash,
        )!!

    /** Render a List<String> as a PostgreSQL `text[]` array literal, or null when empty. */
    private fun toPgTextArray(values: List<String>): String? {
        if (values.isEmpty()) return null
        return values.joinToString(prefix = "{", postfix = "}", separator = ",") { v ->
            "\"" + v.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
        }
    }

    /**
     * Cursor-paginated read of the caller tenant's audit rows (Component 6 read API). Filters by
     * optional time window / event_type / agent_id. [afterId] is the keyset cursor (exclusive);
     * rows are returned id-ascending so the next cursor is the last id. [limit] is clamped 1..500.
     */
    fun list(
        tenantId: UUID,
        from: Instant?,
        to: Instant?,
        eventType: String?,
        agentId: UUID?,
        afterId: Long?,
        limit: Int,
    ): List<AuditRow> {
        val capped = limit.coerceIn(1, 500)
        val sql = StringBuilder("SELECT * FROM auth.audit_log WHERE tenant_id = ?")
        val args = mutableListOf<Any?>(tenantId)
        from?.let { sql.append(" AND created_at >= ?"); args.add(Timestamp.from(it)) }
        to?.let { sql.append(" AND created_at <= ?"); args.add(Timestamp.from(it)) }
        eventType?.let { sql.append(" AND event_type = ?"); args.add(it) }
        agentId?.let { sql.append(" AND agent_id = ?"); args.add(it) }
        afterId?.let { sql.append(" AND id > ?"); args.add(it) }
        sql.append(" ORDER BY id ASC LIMIT ?")
        args.add(capped)
        return tenantTx.inTenant(tenantId) { jdbc -> jdbc.query(sql.toString(), ::mapRow, *args.toTypedArray()) }
    }

    /**
     * Read the full ordered chain for [tenantId] in a window (id-ascending) for the verify
     * endpoint. Bounded by [maxRows] to avoid unbounded scans.
     */
    fun chain(tenantId: UUID, from: Instant?, to: Instant?, maxRows: Int = 100_000): List<AuditRow> {
        val sql = StringBuilder("SELECT * FROM auth.audit_log WHERE tenant_id = ?")
        val args = mutableListOf<Any?>(tenantId)
        from?.let { sql.append(" AND created_at >= ?"); args.add(Timestamp.from(it)) }
        to?.let { sql.append(" AND created_at <= ?"); args.add(Timestamp.from(it)) }
        sql.append(" ORDER BY id ASC LIMIT ?")
        args.add(maxRows)
        return tenantTx.inTenant(tenantId) { jdbc -> jdbc.query(sql.toString(), ::mapRow, *args.toTypedArray()) }
    }

    private fun mapRow(rs: ResultSet, @Suppress("UNUSED_PARAMETER") n: Int): AuditRow {
        @Suppress("UNCHECKED_CAST")
        val policyIds: List<String> =
            (rs.getArray("policy_ids")?.array as? Array<Any?>)?.mapNotNull { it?.toString() } ?: emptyList()
        return AuditRow(
            id = rs.getLong("id"),
            eventType = rs.getString("event_type"),
            agentId = rs.getObject("agent_id", UUID::class.java),
            tenantId = rs.getObject("tenant_id", UUID::class.java),
            action = rs.getString("action"),
            resource = rs.getString("resource"),
            decision = rs.getString("decision"),
            policyIds = policyIds,
            requestId = rs.getObject("request_id", UUID::class.java),
            traceId = rs.getObject("trace_id", UUID::class.java),
            ipAddress = rs.getString("ip_address"),
            createdAt = rs.getTimestamp("created_at").toInstant(),
            rowHash = rs.getBytes("row_hash"),
            prevRowHash = rs.getBytes("prev_row_hash"),
        )
    }
}
