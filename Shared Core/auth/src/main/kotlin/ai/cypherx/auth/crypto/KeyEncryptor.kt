package ai.cypherx.auth.crypto

/**
 * Envelope-encryptor for signing-key private material (Contract 1, Component 3).
 *
 * The private RSA PEM of every `auth.signing_keys` row is stored ONLY in encrypted form
 * (`private_pem_enc BYTEA`). There is NO `JWT_PRIVATE_KEY` env var. Two implementations:
 *
 *  - [LocalAesKeyEncryptor] — AES-256-GCM with a base64 local master key (dev / local stack).
 *  - [KmsKeyEncryptor]      — AWS KMS Encrypt/Decrypt against a Customer-Managed Key (cloud).
 *
 * The bean is selected by `cypherx.auth.key-encryptor` (`local` | `kms`) in
 * [KeyEncryptorConfig]. Inject this interface by constructor; never the concrete type.
 *
 * Both methods operate on raw bytes (the caller decides the encoding — typically the UTF-8
 * bytes of a PKCS#8 PEM string). [encrypt] output is opaque ciphertext (IV/nonce + tag are
 * packed in, format is implementation-private) and round-trips through [decrypt].
 */
interface KeyEncryptor {

    /** Which backend this instance is. Surfaced on /readyz as `encryptor: <kind>`. */
    val kind: String

    /** Cheap readiness probe — true when the encryptor has the key material it needs. */
    fun isReady(): Boolean

    /** Envelope-encrypt [plaintext]; returns opaque ciphertext bytes safe to store in BYTEA. */
    fun encrypt(plaintext: ByteArray): ByteArray

    /** Reverse of [encrypt]. Throws if the ciphertext is corrupt or the key is wrong. */
    fun decrypt(ciphertext: ByteArray): ByteArray
}
