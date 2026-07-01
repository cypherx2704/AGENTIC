package ai.cypherx.auth.crypto

import ai.cypherx.auth.config.AuthProperties
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Dev / local-stack [KeyEncryptor]: AES-256-GCM with a base64-encoded 32-byte master key
 * (`cypherx.auth.local-master-key-b64`). NEVER used in cloud — cloud uses [KmsKeyEncryptor].
 *
 * Wire format of the ciphertext returned by [encrypt] (and consumed by [decrypt]):
 *
 *     [ 1 byte version=1 ][ 12-byte GCM nonce ][ ciphertext+16-byte GCM tag ]
 *
 * The 96-bit nonce is freshly CSPRNG-generated per encryption (GCM nonce-reuse is fatal).
 * The GCM authentication tag (128-bit) is appended by the JCE provider and protects integrity.
 */
class LocalAesKeyEncryptor(props: AuthProperties) : KeyEncryptor {

    override val kind: String = "local"

    private val rng = SecureRandom()
    private val keySpec: SecretKeySpec

    init {
        val b64 = props.localMasterKeyB64
            ?: error("cypherx.auth.local-master-key-b64 is required when key-encryptor=local")
        val raw = Base64.getDecoder().decode(b64.trim())
        require(raw.size == 32) {
            "local-master-key-b64 must decode to exactly 32 bytes (AES-256); got ${raw.size}"
        }
        keySpec = SecretKeySpec(raw, "AES")
    }

    override fun isReady(): Boolean = true

    override fun encrypt(plaintext: ByteArray): ByteArray {
        val nonce = ByteArray(NONCE_LEN).also(rng::nextBytes)
        val cipher = Cipher.getInstance(TRANSFORM).apply {
            init(Cipher.ENCRYPT_MODE, keySpec, GCMParameterSpec(TAG_BITS, nonce))
        }
        val ct = cipher.doFinal(plaintext)
        return ByteArray(1 + NONCE_LEN + ct.size).also { out ->
            out[0] = VERSION
            System.arraycopy(nonce, 0, out, 1, NONCE_LEN)
            System.arraycopy(ct, 0, out, 1 + NONCE_LEN, ct.size)
        }
    }

    override fun decrypt(ciphertext: ByteArray): ByteArray {
        require(ciphertext.size > 1 + NONCE_LEN) { "ciphertext too short" }
        require(ciphertext[0] == VERSION) { "unsupported local-encryptor version byte: ${ciphertext[0]}" }
        val nonce = ciphertext.copyOfRange(1, 1 + NONCE_LEN)
        val body = ciphertext.copyOfRange(1 + NONCE_LEN, ciphertext.size)
        val cipher = Cipher.getInstance(TRANSFORM).apply {
            init(Cipher.DECRYPT_MODE, keySpec, GCMParameterSpec(TAG_BITS, nonce))
        }
        return cipher.doFinal(body)
    }

    private companion object {
        const val TRANSFORM = "AES/GCM/NoPadding"
        const val NONCE_LEN = 12
        const val TAG_BITS = 128
        const val VERSION: Byte = 1
    }
}
