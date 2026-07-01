package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.util.UUID

/**
 * JDBC access to `auth.outbox` — the transactional outbox (Phase 2 Amendment Log 2026-06).
 *
 * The table is PLATFORM-scoped (no RLS — see migration 20260610_0003) so the relay can drain
 * every tenant's rows in one pass.
 *
 * The critical method is [insertInTx]: it takes the CALLER's already-open transactional
 * [JdbcTemplate] (the one a [TenantTx] block hands out) so the outbox row commits/rolls back
 * atomically WITH the state change it describes — that is the whole point of the outbox.
 * Relay-side bookkeeping ([fetchUnpublished] / [markPublished] / [markFailed]) runs in its own
 * short platform transactions.
 */
@Repository
class OutboxRepository(private val tenantTx: TenantTx) {

    /** An unpublished outbox row as the relay sees it. `payloadJson` is the Contract 5 envelope. */
    data class OutboxRow(
        val id: UUID,
        val topic: String,
        val partitionKey: String,
        val payloadJson: String,
        val attempts: Int,
    )

    /**
     * Insert an outbox row INSIDE the caller's open transaction. [jdbc] MUST be the template a
     * surrounding [TenantTx.inPlatform]/[TenantTx.inTenant] block provided — never a fresh one —
     * or the same-transaction guarantee is silently lost.
     */
    fun insertInTx(jdbc: JdbcTemplate, topic: String, partitionKey: String, envelopeJson: String): UUID {
        val id = UUID.randomUUID()
        jdbc.update(
            "INSERT INTO auth.outbox (id, topic, partition_key, payload) VALUES (?, ?, ?, ?::jsonb)",
            id,
            topic,
            partitionKey,
            envelopeJson,
        )
        return id
    }

    /** Oldest-first unpublished rows, at most [limit] (the relay's batch size). */
    fun fetchUnpublished(limit: Int): List<OutboxRow> = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            """
            SELECT id, topic, partition_key, payload::text AS payload, attempts
              FROM auth.outbox
             WHERE published_at IS NULL
             ORDER BY created_at
             LIMIT ?
            """.trimIndent(),
            ROW_MAPPER,
            limit,
        )
    }

    /** Stamp a row published (the relay confirmed the Kafka send). */
    fun markPublished(id: UUID): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update("UPDATE auth.outbox SET published_at = NOW() WHERE id = ?", id)
    }

    /** Record a failed publish attempt: bump `attempts`, keep the (truncated) error for ops. */
    fun markFailed(id: UUID, error: String): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            error.take(MAX_ERROR_LENGTH),
            id,
        )
    }

    /** Unpublished backlog size (relay observability / tests). */
    fun countUnpublished(): Long = tenantTx.inPlatform { jdbc ->
        jdbc.queryForObject("SELECT COUNT(*) FROM auth.outbox WHERE published_at IS NULL", Long::class.java) ?: 0L
    }

    private companion object {
        /** Same truncation bound the other services' outboxes use for `last_error`. */
        const val MAX_ERROR_LENGTH = 2000

        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            OutboxRow(
                id = rs.getObject("id", UUID::class.java),
                topic = rs.getString("topic"),
                partitionKey = rs.getString("partition_key"),
                payloadJson = rs.getString("payload"),
                attempts = rs.getInt("attempts"),
            )
        }
    }
}
