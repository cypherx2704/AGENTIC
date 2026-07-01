package ai.cypherx.auth.signing

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.crypto.KeyEncryptor
import ai.cypherx.auth.domain.SigningKeyStatus
import com.nimbusds.jose.JWSAlgorithm
import com.nimbusds.jose.jwk.KeyUse
import com.nimbusds.jose.jwk.RSAKey
import com.nimbusds.jose.jwk.gen.RSAKeyGenerator
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.nio.charset.StandardCharsets
import java.security.KeyFactory
import java.security.interfaces.RSAPrivateKey
import java.security.interfaces.RSAPublicKey
import java.security.spec.PKCS8EncodedKeySpec
import java.time.Duration
import java.time.Instant
import java.util.Base64
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * Source of truth for the platform's RS256 signing keys (Component 3).
 *
 *  - [ensureBootstrapKey] runs at startup: if `auth.signing_keys` is empty it generates an
 *    RSA-2048 pair, envelope-encrypts the PKCS#8 private PEM via [KeyEncryptor], and inserts a
 *    row with status=signing.
 *  - [activeSigner] returns the kid + full (private) [RSAKey] used to mint tokens.
 *  - [verifiers] returns the public [RSAKey]s of every signing+verifying row (for JWKS and verify).
 *  - [rotate] performs a standard 90-day rotation (old signing -> verifying, new signing inserted).
 *
 * Decrypted private keys live only in the [cache] (kid -> RSAKey), never on disk. The DB stores
 * the public JWK in clear (`public_jwk`) and the private PEM only encrypted (`private_pem_enc`).
 */
@Service
class SigningKeyService(
    private val repo: SigningKeyRepository,
    private val encryptor: KeyEncryptor,
    private val props: AuthProperties,
) {

    /** kid -> reconstructed RSAKey (with private params). In-memory only. */
    private val cache = ConcurrentHashMap<UUID, RSAKey>()

    /** Idempotent bootstrap: generate the first signing key only when the table is empty. */
    @Synchronized
    fun ensureBootstrapKey() {
        if (repo.count() > 0L) {
            log.info("signing_keys present ({} rows); skipping bootstrap", repo.count())
            return
        }
        val generated = generateRsaKey(SigningKeyStatus.SIGNING, promoted = true)
        repo.insert(generated.row)
        cache[generated.row.kid] = generated.rsaKey
        log.info("bootstrap: generated initial signing key kid={}", generated.row.kid)
    }

    /** The active signer: kid (for the JWT header) + the private RSAKey to sign with. */
    fun activeSigner(): ActiveSigner {
        val row = repo.findSigning()
            ?: error("no signing key present — ensureBootstrapKey must run before minting")
        val rsa = loadPrivate(row)
        return ActiveSigner(kid = row.kid, key = rsa)
    }

    /** Public RSAKeys of every signing + verifying row — used to build JWKS and to verify. */
    fun verifiers(): List<RSAKey> =
        repo.listVerifiable().map { parsePublicJwk(it).toPublicJWK() }

    /** Resolve a single verifier public key by kid (refresh-from-DB on miss is the caller's job). */
    fun verifierFor(kid: String): RSAKey? =
        repo.listVerifiable().firstOrNull { it.kid.toString() == kid }?.let { parsePublicJwk(it).toPublicJWK() }

    /**
     * Standard rotation (every [AuthProperties.signingKeyRotationDays] days, or on incident):
     * generate a fresh RSA-2048 key, demote the current signing key to verifying, and promote
     * the new key — atomically (partial unique index). Old kid stays in JWKS to verify in-flight
     * tokens until later retired. Returns the new kid.
     */
    @Synchronized
    fun rotate(): UUID {
        val generated = generateRsaKey(SigningKeyStatus.SIGNING, promoted = true)
        repo.promoteAtomically(generated.row)
        cache[generated.row.kid] = generated.rsaKey
        log.info("rotation: promoted new signing key kid={}", generated.row.kid)
        return generated.row.kid
    }

    /**
     * Emergency rotation (KEY COMPROMISE ONLY). Like [rotate] it generates+promotes a fresh signing
     * key and demotes the current one to `verifying` — but because the old key is presumed
     * compromised, the caller must POISON the old kid (every verifier rejects ANY token it signed,
     * immediately) rather than gracefully waiting out the verification window. We therefore return
     * BOTH kids so the controller can call [ai.cypherx.auth.service.RevocationService.poisonKid] on
     * the old one.
     *
     * @return [EmergencyRotateResult] with the new (now-signing) kid and the old (now-verifying,
     *         to-be-poisoned) kid. [EmergencyRotateResult.oldKid] is null only if the table had no
     *         signing key to demote (a fresh/empty install — should not happen post-bootstrap).
     */
    @Synchronized
    fun emergencyRotate(): EmergencyRotateResult {
        val oldKid = repo.findSigning()?.kid
        val generated = generateRsaKey(SigningKeyStatus.SIGNING, promoted = true)
        repo.promoteAtomically(generated.row)
        cache[generated.row.kid] = generated.rsaKey
        log.warn("emergency rotation: promoted new signing key kid={}, old kid={} to be poisoned", generated.row.kid, oldKid)
        return EmergencyRotateResult(newKid = generated.row.kid, oldKid = oldKid)
    }

    /**
     * Retire a single key by kid (status -> retired, drops it from JWKS). Refuses to retire the
     * current SIGNING key — retiring the active signer would break minting. Idempotent: retiring an
     * already-retired/non-existent kid is a no-op at the DB level.
     */
    @Synchronized
    fun retire(kid: UUID) {
        val signing = repo.findSigning()
        if (signing != null && signing.kid == kid) {
            error("refusing to retire the active signing key kid=$kid")
        }
        repo.retire(kid)
        cache.remove(kid)
        log.info("retired signing key kid={}", kid)
    }

    /**
     * Retire every `verifying` key whose demotion is older than [AuthProperties.verifyingKeyRetentionHours]
     * — i.e. that has remained in JWKS long enough that any in-flight token it signed has already
     * expired (the retention window MUST exceed the max agent-token TTL + clock skew; see
     * [AuthProperties.verifyingKeyRetentionHours]). Removing a verifying key any earlier could reject
     * a still-valid token whose `kid` is no longer published. Returns the number of keys retired.
     */
    @Synchronized
    fun retireExpiredVerifiers(): Int {
        val cutoff = Instant.now().minus(Duration.ofHours(props.verifyingKeyRetentionHours))
        val expired = repo.findVerifyingDemotedBefore(cutoff)
        for (key in expired) {
            repo.retire(key.kid)
            cache.remove(key.kid)
            log.info("retired expired verifying key kid={} (created_at={})", key.kid, key.createdAt)
        }
        if (expired.isNotEmpty()) {
            log.info("retirement sweep retired {} verifying key(s) older than {}h", expired.size, props.verifyingKeyRetentionHours)
        }
        return expired.size
    }

    /** Snapshot of all keys for the operability list endpoint. */
    fun listAll(): List<SigningKey> = repo.findByStatus(SigningKeyStatus.SIGNING) +
        repo.findByStatus(SigningKeyStatus.VERIFYING) +
        repo.findByStatus(SigningKeyStatus.RETIRED)

    // ─────────────────────────────────────────────────────────────────────────────────────

    /** Load (and cache) the private RSAKey for [row], decrypting its private PEM on first use. */
    private fun loadPrivate(row: SigningKey): RSAKey = cache.computeIfAbsent(row.kid) {
        val pemBytes = encryptor.decrypt(row.privatePemEnc)
        val pem = String(pemBytes, StandardCharsets.UTF_8)
        val privateKey = parsePkcs8(pem)
        val publicJwk = parsePublicJwk(row)
        RSAKey.Builder(publicJwk)
            .privateKey(privateKey)
            .build()
    }

    private fun parsePublicJwk(row: SigningKey): RSAKey = RSAKey.parse(row.publicJwk)

    private data class Generated(val row: SigningKey, val rsaKey: RSAKey)

    private fun generateRsaKey(status: SigningKeyStatus, promoted: Boolean): Generated {
        val kid = UUID.randomUUID()
        val rsaKey: RSAKey = RSAKeyGenerator(RSA_KEY_SIZE)
            .keyUse(KeyUse.SIGNATURE)
            .algorithm(JWSAlgorithm.RS256)
            .keyID(kid.toString())
            .generate()

        val publicJwkJson = rsaKey.toPublicJWK().toJSONString()
        val privatePem = toPkcs8Pem((rsaKey.toPrivateKey() as RSAPrivateKey))
        val enc = encryptor.encrypt(privatePem.toByteArray(StandardCharsets.UTF_8))

        val now = Instant.now()
        val row = SigningKey(
            kid = kid,
            status = status,
            publicJwk = publicJwkJson,
            privatePemEnc = enc,
            createdAt = now,
            promotedAt = if (promoted) now else null,
            retiredAt = null,
        )
        return Generated(row, rsaKey)
    }

    /** kid + RSAKey-with-private-params ready to feed an RSASSASigner. */
    data class ActiveSigner(val kid: UUID, val key: RSAKey)

    /**
     * Result of [emergencyRotate]: the freshly-promoted signing [newKid] and the demoted, presumed-
     * compromised [oldKid] the caller must poison (null only on an empty-table edge case).
     */
    data class EmergencyRotateResult(val newKid: UUID, val oldKid: UUID?)

    private companion object {
        val log = LoggerFactory.getLogger(SigningKeyService::class.java)
        const val RSA_KEY_SIZE = 2048

        private val PEM_ENCODER = Base64.getMimeEncoder(64, "\n".toByteArray())

        fun toPkcs8Pem(privateKey: RSAPrivateKey): String {
            val b64 = PEM_ENCODER.encodeToString(privateKey.encoded)
            return "-----BEGIN PRIVATE KEY-----\n$b64\n-----END PRIVATE KEY-----\n"
        }

        fun parsePkcs8(pem: String): RSAPrivateKey {
            val body = pem
                .replace("-----BEGIN PRIVATE KEY-----", "")
                .replace("-----END PRIVATE KEY-----", "")
                .replace("\\s".toRegex(), "")
            val der = Base64.getDecoder().decode(body)
            val kf = KeyFactory.getInstance("RSA")
            return kf.generatePrivate(PKCS8EncodedKeySpec(der)) as RSAPrivateKey
        }

        @Suppress("unused")
        fun rsaPublic(key: RSAKey): RSAPublicKey = key.toRSAPublicKey()
    }
}
