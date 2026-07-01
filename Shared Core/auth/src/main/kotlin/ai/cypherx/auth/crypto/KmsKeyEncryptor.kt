package ai.cypherx.auth.crypto

import ai.cypherx.auth.config.AuthProperties
import org.slf4j.LoggerFactory
import software.amazon.awssdk.core.SdkBytes
import software.amazon.awssdk.services.kms.KmsClient
import software.amazon.awssdk.services.kms.model.DecryptRequest
import software.amazon.awssdk.services.kms.model.EncryptRequest

/**
 * Cloud [KeyEncryptor]: AWS KMS Encrypt/Decrypt against the per-environment Customer-Managed
 * Key (`cypherx.auth.kms-signing-cmk-arn`). The signing-key private PEM never leaves the
 * process in clear and is never written to disk; KMS holds the wrapping key (Component 3).
 *
 * The [KmsClient] is created from the default AWS credential + region provider chain (IRSA in
 * EKS). [encrypt] returns the KMS ciphertext blob verbatim; [decrypt] hands it back to KMS.
 * On KMS outage callers fall back to in-memory cached decrypted keys (SigningKeyService),
 * so this class simply propagates the SDK exception.
 */
class KmsKeyEncryptor(
    private val kms: KmsClient,
    private val cmkArn: String,
) : KeyEncryptor {

    override val kind: String = "kms"

    constructor(props: AuthProperties) : this(
        KmsClient.create(),
        props.kmsSigningCmkArn
            ?: error("cypherx.auth.kms-signing-cmk-arn is required when key-encryptor=kms"),
    )

    override fun isReady(): Boolean = cmkArn.isNotBlank()

    override fun encrypt(plaintext: ByteArray): ByteArray {
        val resp = kms.encrypt(
            EncryptRequest.builder()
                .keyId(cmkArn)
                .plaintext(SdkBytes.fromByteArray(plaintext))
                .build(),
        )
        return resp.ciphertextBlob().asByteArray()
    }

    override fun decrypt(ciphertext: ByteArray): ByteArray {
        val resp = kms.decrypt(
            DecryptRequest.builder()
                .keyId(cmkArn)
                .ciphertextBlob(SdkBytes.fromByteArray(ciphertext))
                .build(),
        )
        return resp.plaintext().asByteArray()
    }

    private companion object {
        @Suppress("unused")
        val log = LoggerFactory.getLogger(KmsKeyEncryptor::class.java)
    }
}
