package ai.cypherx.auth.signing

import org.slf4j.LoggerFactory
import org.springframework.boot.context.event.ApplicationReadyEvent
import org.springframework.context.event.EventListener
import org.springframework.stereotype.Component

/**
 * Runs [SigningKeyService.ensureBootstrapKey] once the application context is ready and the
 * DataSource is live. Generating the first signing key at startup means /token, JWKS, and OIDC
 * discovery work immediately on a fresh install (Component 3 bootstrap).
 *
 * Failure here is logged but NOT fatal — on a transient DB blip at boot the key can be created
 * lazily on first mint; /readyz reports not-ready until a signing key exists.
 */
@Component
class SigningKeyBootstrap(private val signingKeyService: SigningKeyService) {

    @EventListener(ApplicationReadyEvent::class)
    fun onReady() {
        try {
            signingKeyService.ensureBootstrapKey()
        } catch (ex: Exception) {
            log.warn("signing-key bootstrap deferred (will retry on first mint): {}", ex.message)
        }
    }

    private companion object {
        val log = LoggerFactory.getLogger(SigningKeyBootstrap::class.java)
    }
}
