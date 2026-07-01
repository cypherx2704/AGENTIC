package ai.cypherx.auth.signing

import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.SigningKeyStatus
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.RowMapper
import org.springframework.stereotype.Repository
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

/**
 * JDBC access to `auth.signing_keys` (platform-scoped — NO RLS, accessed via
 * [TenantTx.inPlatform]). Holds the RS256 key material; the private PEM is stored only
 * envelope-encrypted (`private_pem_enc`).
 *
 * The partial unique index `one_signing_key WHERE status='signing'` guarantees at most one
 * row in `signing` state, which makes the rotation swap in [promoteAtomically] safe.
 */
@Repository
class SigningKeyRepository(private val tenantTx: TenantTx) {

    /** Insert a brand-new key row (used by bootstrap and rotation). */
    fun insert(key: SigningKey): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            """
            INSERT INTO auth.signing_keys
              (kid, private_pem_enc, public_jwk, status, created_at, promoted_at, retired_at)
            VALUES (?, ?, ?::jsonb, ?, ?, ?, ?)
            """.trimIndent(),
            key.kid,
            key.privatePemEnc,
            key.publicJwk,
            key.status.value,
            Timestamp.from(key.createdAt),
            key.promotedAt?.let(Timestamp::from),
            key.retiredAt?.let(Timestamp::from),
        )
    }

    /** Count all rows (used to detect a fresh install for bootstrap). */
    fun count(): Long = tenantTx.inPlatform { jdbc ->
        jdbc.queryForObject("SELECT count(*) FROM auth.signing_keys", Long::class.java) ?: 0L
    }

    /** The single key in [SigningKeyStatus.SIGNING] state, or null if none. */
    fun findSigning(): SigningKey? = findByStatus(SigningKeyStatus.SIGNING).firstOrNull()

    /** All rows currently in the given [status]. */
    fun findByStatus(status: SigningKeyStatus): List<SigningKey> = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT * FROM auth.signing_keys WHERE status = ? ORDER BY created_at DESC",
            ROW_MAPPER,
            status.value,
        )
    }

    /**
     * Keys usable to VERIFY a token: `signing` + `verifying` (NOT `retired`).
     * This is the set published in JWKS and used by [JwtMintService.verify].
     */
    fun listVerifiable(): List<SigningKey> = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            "SELECT * FROM auth.signing_keys WHERE status IN ('signing','verifying') ORDER BY created_at DESC",
            ROW_MAPPER,
        )
    }

    /**
     * Atomic standard-rotation swap, in ONE transaction, leaning on the partial unique index:
     *   - the current `signing` key (if any) becomes `verifying`,
     *   - [newKey] (which MUST carry status=signing) is inserted as the new `signing` key.
     * No window with zero signing keys exists.
     */
    fun promoteAtomically(newKey: SigningKey): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.signing_keys SET status = 'verifying' WHERE status = 'signing'",
        )
        jdbc.update(
            """
            INSERT INTO auth.signing_keys
              (kid, private_pem_enc, public_jwk, status, created_at, promoted_at, retired_at)
            VALUES (?, ?, ?::jsonb, 'signing', ?, ?, NULL)
            """.trimIndent(),
            newKey.kid,
            newKey.privatePemEnc,
            newKey.publicJwk,
            Timestamp.from(newKey.createdAt),
            Timestamp.from(newKey.promotedAt ?: newKey.createdAt),
        )
    }

    /** Mark a `verifying` key `retired` (called after max-TTL + buffer once tokens have aged out). */
    fun retire(kid: UUID): Unit = tenantTx.inPlatform { jdbc ->
        jdbc.update(
            "UPDATE auth.signing_keys SET status = 'retired', retired_at = NOW() WHERE kid = ?",
            kid,
        )
    }

    /**
     * Find `verifying` keys whose DEMOTION is provably older than [cutoff] — i.e. keys that have
     * sat in JWKS long enough that every token they could have signed has expired, so they are safe
     * to retire.
     *
     * The schema has no `demoted_at` column: a key is demoted (signing -> verifying) atomically the
     * instant its SUCCESSOR is promoted ([promoteAtomically]). So for any currently-`verifying` key K
     * we have `demoted_at(K) <= promoted_at(current signing key)`. We therefore use the CURRENT
     * signing key's `promoted_at` as the conservative upper bound on every verifying key's demotion
     * time. Retiring only when `currentSigning.promoted_at < cutoff` guarantees we NEVER retire a key
     * while a token it signed could still validate (we err strictly on keeping keys verifying LONGER,
     * never shorter). If there is no signing key (should not happen post-bootstrap) nothing is
     * returned.
     */
    fun findVerifyingDemotedBefore(cutoff: Instant): List<SigningKey> = tenantTx.inPlatform { jdbc ->
        jdbc.query(
            """
            SELECT v.*
              FROM auth.signing_keys v
             WHERE v.status = 'verifying'
               AND EXISTS (
                     SELECT 1
                       FROM auth.signing_keys s
                      WHERE s.status = 'signing'
                        AND s.promoted_at IS NOT NULL
                        AND s.promoted_at < ?
                   )
             ORDER BY v.created_at ASC
            """.trimIndent(),
            ROW_MAPPER,
            Timestamp.from(cutoff),
        )
    }

    private companion object {
        val ROW_MAPPER = RowMapper { rs: ResultSet, _: Int ->
            SigningKey(
                kid = rs.getObject("kid", UUID::class.java),
                status = SigningKeyStatus.from(rs.getString("status")),
                publicJwk = rs.getString("public_jwk"),
                privatePemEnc = rs.getBytes("private_pem_enc"),
                createdAt = rs.getTimestamp("created_at").toInstant(),
                promotedAt = rs.getTimestamp("promoted_at")?.toInstant(),
                retiredAt = rs.getTimestamp("retired_at")?.toInstant(),
            )
        }

        @Suppress("unused")
        fun nowTs(): Timestamp = Timestamp.from(Instant.now())
    }
}
