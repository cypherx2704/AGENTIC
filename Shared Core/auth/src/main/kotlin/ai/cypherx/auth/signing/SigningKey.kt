package ai.cypherx.auth.signing

import ai.cypherx.auth.domain.SigningKeyStatus
import java.time.Instant
import java.util.UUID

/**
 * One row of `auth.signing_keys` (platform-scoped, no RLS).
 *
 * @param kid           UUID primary key; also the JWT header `kid` and the JWK `kid`.
 * @param status        signing | verifying | retired (exactly one `signing` at a time).
 * @param publicJwk     the public key as a JWK JSON string (clear; served via JWKS).
 * @param privatePemEnc envelope-encrypted private PKCS#8 PEM bytes (KeyEncryptor output).
 * @param createdAt     row creation time.
 * @param promotedAt    when it became the active signing key (null until promoted).
 * @param retiredAt     when it was retired (null while signing/verifying).
 */
data class SigningKey(
    val kid: UUID,
    val status: SigningKeyStatus,
    val publicJwk: String,
    val privatePemEnc: ByteArray,
    val createdAt: Instant,
    val promotedAt: Instant? = null,
    val retiredAt: Instant? = null,
) {
    // ByteArray breaks data-class equality; override to compare by kid (the identity).
    override fun equals(other: Any?): Boolean = this === other || (other is SigningKey && other.kid == kid)
    override fun hashCode(): Int = kid.hashCode()
}
