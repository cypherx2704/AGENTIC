package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.time.Instant
import java.util.UUID

/**
 * In-memory projection of an `auth.users` row — an end-user login identity (email/password or
 * Google OAuth). NOTE: `password_hash` and `google_sub` are projected here because login verifies
 * against them; this row is never serialised to a client.
 */
data class UserRecord(
    val userId: UUID,
    val tenantId: UUID,
    val email: String,
    val passwordHash: String?,
    val loginProvider: String,
    val googleSub: String?,
    val displayName: String?,
    val status: String,
    val emailVerified: Boolean,
    val lastLoginAt: Instant?,
    val createdAt: Instant,
)

/**
 * JDBC access to `auth.users`.
 *
 * The table is PLATFORM-scoped (NO RLS) — login resolves a user by email BEFORE any tenant context
 * exists, exactly like `auth.signup_attempts`. Every access therefore goes through
 * [TenantTx.inPlatform] (a plain transaction with no `app.tenant_id`). Plain JdbcTemplate, no JPA.
 */
@Repository
class UserRepository(private val tenantTx: TenantTx) {

    /** Insert a new user and return the persisted row. Uniqueness on email is enforced by the DB. */
    fun insert(
        tenantId: UUID,
        email: String,
        passwordHash: String?,
        loginProvider: String,
        googleSub: String?,
        displayName: String?,
        emailVerified: Boolean,
    ): UserRecord = tenantTx.inPlatform { jdbc ->
        val id = UUID.randomUUID()
        jdbc.update(
            """
            INSERT INTO auth.users
              (user_id, tenant_id, email, password_hash, login_provider, google_sub,
               display_name, email_verified, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NOW(), NOW())
            """.trimIndent(),
            id,
            tenantId,
            email,
            passwordHash,
            loginProvider,
            googleSub,
            displayName,
            emailVerified,
        )
        jdbc.queryForObject(SELECT_BY_ID, ROW_MAPPER, id)!!
    }

    /** Find a user by (case-insensitive) email, or null. Drives email/password login. */
    fun findByEmail(email: String): UserRecord? = tenantTx.inPlatform { jdbc ->
        jdbc.query("$SELECT_BASE WHERE email = ?", ROW_MAPPER, email).firstOrNull()
    }

    /** Find a user by Google OIDC subject, or null. Drives Google login. */
    fun findByGoogleSub(googleSub: String): UserRecord? = tenantTx.inPlatform { jdbc ->
        jdbc.query("$SELECT_BASE WHERE google_sub = ?", ROW_MAPPER, googleSub).firstOrNull()
    }

    /** Stamp `last_login_at = NOW()` (best-effort; the caller ignores failures). */
    fun touchLastLogin(userId: UUID): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update("UPDATE auth.users SET last_login_at = NOW(), updated_at = NOW() WHERE user_id = ?", userId)
    }

    /** Link a Google subject to an existing (local) user — first time they use Google SSO. */
    fun linkGoogleSub(userId: UUID, googleSub: String): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.users SET google_sub = ?, email_verified = true, updated_at = NOW() WHERE user_id = ?",
            googleSub,
            userId,
        )
    }

    private companion object {
        const val SELECT_BASE = """
            SELECT user_id, tenant_id, email, password_hash, login_provider, google_sub,
                   display_name, status, email_verified, last_login_at, created_at
              FROM auth.users
        """
        const val SELECT_BY_ID = "$SELECT_BASE WHERE user_id = ?"

        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            UserRecord(
                userId = rs.getObject("user_id", UUID::class.java),
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                email = rs.getString("email"),
                passwordHash = rs.getString("password_hash"),
                loginProvider = rs.getString("login_provider"),
                googleSub = rs.getString("google_sub"),
                displayName = rs.getString("display_name"),
                status = rs.getString("status"),
                emailVerified = rs.getBoolean("email_verified"),
                lastLoginAt = rs.getTimestamp("last_login_at")?.toInstant(),
                createdAt = rs.getTimestamp("created_at").toInstant(),
            )
        }
    }
}
