package ai.cypherx.auth.crypto

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.KeyEncryptorKind
import org.slf4j.LoggerFactory
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration

/**
 * Selects the single [KeyEncryptor] bean from `cypherx.auth.key-encryptor`
 * (`local` -> [LocalAesKeyEncryptor], `kms` -> [KmsKeyEncryptor]).
 *
 * One bean only — downstream code injects [KeyEncryptor] by constructor and is agnostic to
 * the backend. Selection happens here (not via @ConditionalOnProperty on each impl) so the
 * KMS client is only constructed when actually selected — local/dev runs never touch the AWS SDK.
 */
@Configuration
class KeyEncryptorConfig {

    @Bean
    fun keyEncryptor(props: AuthProperties): KeyEncryptor =
        when (KeyEncryptorKind.from(props.keyEncryptor)) {
            KeyEncryptorKind.LOCAL -> LocalAesKeyEncryptor(props).also {
                log.info("KeyEncryptor backend = local (AES-256-GCM, dev only)")
            }
            KeyEncryptorKind.KMS -> KmsKeyEncryptor(props).also {
                log.info("KeyEncryptor backend = kms (CMK={})", props.kmsSigningCmkArn)
            }
        }

    private companion object {
        val log = LoggerFactory.getLogger(KeyEncryptorConfig::class.java)
    }
}
