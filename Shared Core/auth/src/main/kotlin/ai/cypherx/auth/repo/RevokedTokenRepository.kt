package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * Durable record of revoked token `jti`s — `auth.revoked_tokens` (Component 3c).
 *
 * This table is PLATFORM-scoped (no `app.tenant_id` / RLS) per the phase doc's table-scope
 * annotations, so all access goes through [TenantTx.inPlatform]. It is the system-of-record behind
 * the Valkey `jti-revoked:{jti}` hot-path set and the `cypherx.auth.token.revoked` Kafka topic: if
 * Valkey is cold (restart), verifiers can rebuild their deny-set from here / from Kafka replay.
 *
 * Rows are insert-once (idempotent on the `jti` PK) and purged after `token_exp` by the hourly
 * purge job — never updated.
 */
@Repository
class RevokedTokenRepository(
    private val tenantTx: TenantTx,
) {

    data class RevokedToken(
        val jti: UUID,
        val agentId: UUID?,
        val tenantId: UUID,
        val revokedAt: Instant,
        val revokedBy: UUID,
        val reason: String,
        val tokenExp: Instant,
    )

    /**
     * Insert a revocation record. Idempotent: a duplicate `jti` is ignored (the token is already
     * revoked). Returns true when a NEW row was inserted, false when it already existed.
     */
    fun insert(
        jti: UUID,
        agentId: UUID?,
        tenantId: UUID,
        revokedBy: UUID,
        reason: String,
        tokenExp: Instant,
        revokedAt: Instant = Instant.now(),
    ): Boolean = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            INSERT INTO auth.revoked_tokens (jti, agent_id, tenant_id, revoked_at, revoked_by, reason, token_exp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (jti) DO NOTHING
            """.trimIndent(),
            jti,
            agentId,
            tenantId,
            Timestamp.from(revokedAt),
            revokedBy,
            reason,
            Timestamp.from(tokenExp),
        ) > 0
    }

    /** True if [jti] has a (not-yet-purged) revocation row. Durable fallback for the Valkey check. */
    fun isRevoked(jti: UUID): Boolean = tenantTx.inPlatform { jdbc ->
        jdbc.queryForObject(
            "SELECT EXISTS(SELECT 1 FROM auth.revoked_tokens WHERE jti = ?)",
            Boolean::class.java,
            jti,
        ) ?: false
    }

    /** Lookup a single revocation record (for diagnostics / audit). */
    fun find(jti: UUID): RevokedToken? = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT * FROM auth.revoked_tokens WHERE jti = ?",
            ::mapRow,
            jti,
        ).firstOrNull()
    }

    /**
     * All not-yet-expired revoked jtis — used to prime a verifier's bloom filter / Valkey deny-set
     * on startup (Component 3c "bloom-filter primed from Kafka replay on startup" durable backstop).
     */
    fun listActive(now: Instant = Instant.now()): List<UUID> = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT jti FROM auth.revoked_tokens WHERE token_exp > ?",
            { rs, _ -> rs.getObject("jti", UUID::class.java) },
            Timestamp.from(now),
        )
    }

    /**
     * Hourly purge job target: delete rows whose token has been expired for more than the +1h
     * forensic buffer (phase doc). Returns the number of rows removed. (DELETE is granted to the
     * runtime role on this platform table — unlike auth.audit_log.)
     */
    fun purgeExpired(olderThan: Instant): Int = tenantTx.inPlatform { jdbc ->
        jdbc.update("DELETE FROM auth.revoked_tokens WHERE token_exp < ?", Timestamp.from(olderThan))
    }

    private fun mapRow(rs: ResultSet, @Suppress("UNUSED_PARAMETER") n: Int): RevokedToken = RevokedToken(
        jti = rs.getObject("jti", UUID::class.java),
        agentId = rs.getObject("agent_id", UUID::class.java),
        tenantId = rs.getObject("tenant_id", UUID::class.java),
        revokedAt = rs.getTimestamp("revoked_at").toInstant(),
        revokedBy = rs.getObject("revoked_by", UUID::class.java),
        reason = rs.getString("reason"),
        tokenExp = rs.getTimestamp("token_exp").toInstant(),
    )
}
