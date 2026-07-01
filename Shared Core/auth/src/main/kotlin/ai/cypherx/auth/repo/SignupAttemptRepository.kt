package ai.cypherx.auth.repo

import ai.cypherx.auth.db.TenantTx
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.time.Instant
import java.util.UUID

/**
 * In-memory projection of an `auth.signup_attempts` row (Component 1c — self-serve onboarding).
 * Only the columns the funnel reads/writes are projected. The raw verification token is NEVER
 * stored or surfaced — only [verificationTokenHash] (SHA-256 hex), mirroring `api_keys.key_hash`.
 */
data class SignupAttempt(
    val signupId: UUID,
    val email: String,
    val tenantName: String?,
    val status: String,
    val verificationTokenHash: String?,
    val verificationExpiresAt: Instant,
    val verifiedAt: Instant?,
    val tenantId: UUID?,
    val riskScore: Double,
    val attempts: Int,
    val ipAddress: String?,
    val createdAt: Instant,
)

/**
 * JDBC access to `auth.signup_attempts` (Component 1c).
 *
 * The table is PLATFORM-scoped (NO RLS — verification is bound to an opaque token, not a tenant
 * JWT; there is no `app.tenant_id` to scope by during signup/verify), so every access goes through
 * [TenantTx.inPlatform] — a plain transaction with NO `app.tenant_id` set. Plain JdbcTemplate on
 * the tx-bound connection (no JPA), matching the rest of the service.
 */
@Repository
class SignupAttemptRepository(private val tenantTx: TenantTx) {

    /**
     * Insert a new pending signup attempt and return the persisted row. The caller supplies the
     * SHA-256 hex of the raw verification token ([verificationTokenHash]); the raw token is emailed
     * and then discarded. `status` defaults to `pending_verification`; `attempts` starts at 1.
     */
    fun insert(
        email: String,
        tenantName: String?,
        verificationTokenHash: String,
        verificationExpiresAt: Instant,
        riskScore: Double,
        status: String,
        ipAddress: String?,
        userAgent: String?,
    ): SignupAttempt = tenantTx.inPlatform { jdbc ->
        val id = UUID.randomUUID()
        jdbc.update(
            """
            INSERT INTO auth.signup_attempts
              (signup_id, email, tenant_name, verification_token_hash, verification_expires_at,
               risk_score, status, attempts, ip_address, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?::inet, ?, NOW())
            """.trimIndent(),
            id,
            email,
            tenantName,
            verificationTokenHash,
            java.sql.Timestamp.from(verificationExpiresAt),
            riskScore,
            status,
            ipAddress,
            userAgent,
        )
        jdbc.queryForObject(SELECT_BY_ID, ROW_MAPPER, id)!!
    }

    /** Find a pending/most-recent attempt by the verification token hash, or null when absent. */
    fun findByTokenHash(verificationTokenHash: String): SignupAttempt? = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "$SELECT_BASE WHERE verification_token_hash = ?",
            ROW_MAPPER,
            verificationTokenHash,
        ).firstOrNull()
    }

    /** Find the most-recent attempt for an email (any status), or null. Drives resend. */
    fun findLatestByEmail(email: String): SignupAttempt? = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "$SELECT_BASE WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            ROW_MAPPER,
            email,
        ).firstOrNull()
    }

    /** Count attempts from [ipAddress] created at/after [since] (velocity scoring). */
    fun countByIpSince(ipAddress: String, since: Instant): Long = tenantTx.inPlatform { jdbc ->
        jdbc.queryForObject(
            "SELECT COUNT(*) FROM auth.signup_attempts WHERE ip_address = ?::inet AND created_at >= ?",
            Long::class.java,
            ipAddress,
            java.sql.Timestamp.from(since),
        ) ?: 0L
    }

    /** Count attempts for [email] created at/after [since] (velocity scoring). */
    fun countByEmailSince(email: String, since: Instant): Long = tenantTx.inPlatform { jdbc ->
        jdbc.queryForObject(
            "SELECT COUNT(*) FROM auth.signup_attempts WHERE email = ? AND created_at >= ?",
            Long::class.java,
            email,
            java.sql.Timestamp.from(since),
        ) ?: 0L
    }

    /**
     * Rotate the verification token for a resend: set a fresh [verificationTokenHash] + expiry, bump
     * `attempts`, and keep the row `pending_verification`. Returns the refreshed row, or null if the
     * signup is gone / not pending. Only pending rows are eligible (a verified row cannot be resent).
     */
    fun rotateTokenForResend(
        signupId: UUID,
        verificationTokenHash: String,
        verificationExpiresAt: Instant,
    ): SignupAttempt? = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            UPDATE auth.signup_attempts
               SET verification_token_hash = ?,
                   verification_expires_at = ?,
                   attempts = attempts + 1,
                   status = 'pending_verification'
             WHERE signup_id = ? AND status = 'pending_verification'
            """.trimIndent(),
            verificationTokenHash,
            java.sql.Timestamp.from(verificationExpiresAt),
            signupId,
        )
        jdbc.query(SELECT_BY_ID, ROW_MAPPER, signupId).firstOrNull()
    }

    /**
     * Phase 1 of verification: atomically claim a pending attempt by flipping
     * `pending_verification -> verifying`. The `status = 'pending_verification'` guard makes this a
     * single-winner update — a concurrent double-verify affects 0 rows. Returns true iff THIS call
     * won the claim, so ONLY the winner proceeds to provision the tenant (no orphan tenants on a
     * race). A crashed `verifying` row is recoverable by a future resend/expiry sweep.
     */
    fun claimForVerification(signupId: UUID): Boolean = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.signup_attempts SET status = 'verifying' WHERE signup_id = ? AND status = 'pending_verification'",
            signupId,
        ) == 1
    }

    /**
     * Phase 2 of verification: record the provisioned [tenantId] + [initialAdminUserId] and flip
     * `verifying -> verified`, stamping `verified_at`. Called only by the winner of
     * [claimForVerification].
     */
    fun attachProvisionedTenant(signupId: UUID, tenantId: UUID, initialAdminUserId: UUID?): Unit =
        tenantTx.inPlatform { jdbc ->
            jdbc.update(
                """
                UPDATE auth.signup_attempts
                   SET status = 'verified',
                       verified_at = NOW(),
                       tenant_id = ?,
                       initial_admin_user_id = ?
                 WHERE signup_id = ? AND status = 'verifying'
                """.trimIndent(),
                tenantId,
                initialAdminUserId,
                signupId,
            )
        }

    /** Flip a pending attempt to `expired` (lazy expiry on a verify against a stale token). */
    fun markExpired(signupId: UUID): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.signup_attempts SET status = 'expired' WHERE signup_id = ? AND status = 'pending_verification'",
            signupId,
        )
    }

    private companion object {
        const val SELECT_BASE = """
            SELECT signup_id, email, tenant_name, status, verification_token_hash,
                   verification_expires_at, verified_at, tenant_id, risk_score, attempts,
                   host(ip_address) AS ip_address, created_at
              FROM auth.signup_attempts
        """

        const val SELECT_BY_ID = "$SELECT_BASE WHERE signup_id = ?"

        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            SignupAttempt(
                signupId = rs.getObject("signup_id", UUID::class.java),
                email = rs.getString("email"),
                tenantName = rs.getString("tenant_name"),
                status = rs.getString("status"),
                verificationTokenHash = rs.getString("verification_token_hash"),
                verificationExpiresAt = rs.getTimestamp("verification_expires_at").toInstant(),
                verifiedAt = rs.getTimestamp("verified_at")?.toInstant(),
                tenantId = rs.getObject("tenant_id", UUID::class.java),
                riskScore = rs.getBigDecimal("risk_score")?.toDouble() ?: 0.0,
                attempts = rs.getInt("attempts"),
                ipAddress = rs.getString("ip_address"),
                createdAt = rs.getTimestamp("created_at").toInstant(),
            )
        }
    }
}
