package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * In-memory projection of an `auth.user_sessions` row — one end-user login session backing a
 * refresh token. Never serialised to a client; `refreshTokenHash` is the SHA-256 of the opaque
 * secret, never the raw token.
 */
data class UserSession(
    val sessionId: UUID,
    val userId: UUID,
    val tenantId: UUID,
    val refreshTokenHash: String,
    val issuedAt: Instant,
    val lastUsedAt: Instant,
    val absoluteExpiresAt: Instant,
    val idleTimeoutSeconds: Long,
    val revokedAt: Instant?,
)

/**
 * JDBC access to `auth.user_sessions`.
 *
 * The table is PLATFORM-scoped (NO RLS) — a refresh (like a login) resolves the session/user BEFORE
 * any tenant context exists, exactly like [UserRepository]/`auth.signup_attempts`. Every access goes
 * through [TenantTx.inPlatform] (a plain transaction with no `app.tenant_id`). Plain JdbcTemplate.
 */
@Repository
class UserSessionRepository(private val tenantTx: TenantTx) {

    /**
     * Persist a new session. The caller supplies the pre-computed [refreshTokenHash] (SHA-256 hex),
     * the [absoluteExpiresAt] hard cap, and the [idleTimeoutSeconds] sliding window. Returns the row.
     */
    fun create(
        sessionId: UUID,
        userId: UUID,
        tenantId: UUID,
        refreshTokenHash: String,
        absoluteExpiresAt: Instant,
        idleTimeoutSeconds: Long,
        userAgent: String? = null,
        ipAddress: String? = null,
    ): UserSession = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            INSERT INTO auth.user_sessions
              (session_id, user_id, tenant_id, refresh_token_hash, issued_at, last_used_at,
               absolute_expires_at, idle_timeout_seconds, user_agent, ip_address, created_at)
            VALUES (?, ?, ?, ?, NOW(), NOW(), ?, ?, ?, ?, NOW())
            """.trimIndent(),
            sessionId,
            userId,
            tenantId,
            refreshTokenHash,
            Timestamp.from(absoluteExpiresAt),
            idleTimeoutSeconds,
            userAgent,
            ipAddress,
        )
        jdbc.queryForObject(SELECT_BY_ID, ROW_MAPPER, sessionId)!!
    }

    /** Load a session by its id (the public part of the refresh token), or null. */
    fun findById(sessionId: UUID): UserSession? = tenantTx.inPlatform { jdbc ->
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, sessionId).firstOrNull()
    }

    /** Slide the idle window forward on a successful refresh. Best-effort (single-row UPDATE). */
    fun touchLastUsed(sessionId: UUID): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update("UPDATE auth.user_sessions SET last_used_at = NOW() WHERE session_id = ?", sessionId)
    }

    /** Revoke one session (logout). Idempotent; only affects a still-active row. Returns rows updated. */
    fun revoke(sessionId: UUID, reason: String): Int = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.user_sessions SET revoked_at = NOW(), revoked_reason = ? WHERE session_id = ? AND revoked_at IS NULL",
            reason,
            sessionId,
        )
    }

    /** Revoke every active session for a user (e.g. suspend / password change). Returns rows updated. */
    fun revokeAllForUser(userId: UUID, reason: String): Int = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.user_sessions SET revoked_at = NOW(), revoked_reason = ? WHERE user_id = ? AND revoked_at IS NULL",
            reason,
            userId,
        )
    }

    /** Housekeeping: delete rows past their absolute cap. Returns rows deleted. */
    fun deleteExpired(): Int = tenantTx.inPlatform { jdbc ->
        jdbc.update("DELETE FROM auth.user_sessions WHERE absolute_expires_at < NOW()")
    }

    private companion object {
        const val SELECT_BY_ID = """
            SELECT session_id, user_id, tenant_id, refresh_token_hash, issued_at, last_used_at,
                   absolute_expires_at, idle_timeout_seconds, revoked_at
              FROM auth.user_sessions
             WHERE session_id = ?
        """

        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            UserSession(
                sessionId = rs.getObject("session_id", UUID::class.java),
                userId = rs.getObject("user_id", UUID::class.java),
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                refreshTokenHash = rs.getString("refresh_token_hash"),
                issuedAt = rs.getTimestamp("issued_at").toInstant(),
                lastUsedAt = rs.getTimestamp("last_used_at").toInstant(),
                absoluteExpiresAt = rs.getTimestamp("absolute_expires_at").toInstant(),
                idleTimeoutSeconds = rs.getLong("idle_timeout_seconds"),
                revokedAt = rs.getTimestamp("revoked_at")?.toInstant(),
            )
        }
    }
}
