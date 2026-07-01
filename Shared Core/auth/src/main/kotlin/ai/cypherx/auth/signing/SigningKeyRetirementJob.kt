package ai.cypherx.auth.signing

import org.slf4j.LoggerFactory
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component

/**
 * Periodic sweep that retires demoted signing keys once they have aged out of the verification
 * window (Component 3 key-lifecycle, WP03).
 *
 * A standard rotation demotes the old signing key to `verifying`: it stays in JWKS so in-flight
 * tokens it signed still validate. After
 * [ai.cypherx.auth.config.AuthProperties.verifyingKeyRetentionHours] — which is configured to
 * comfortably exceed the max agent-token TTL (<=1h) plus clock skew — every such token has expired,
 * and the key can be safely retired (dropped from JWKS). [SigningKeyService.retireExpiredVerifiers]
 * is itself the safety boundary; this job just drives it on a cadence.
 *
 * Cadence: fixed delay `cypherx.auth.verifying-key-retirement-sweep-ms` (default 1h). Retirement is
 * not time-critical (correctness is guaranteed by the retention window, not by sweep promptness), so
 * a coarse interval is intentional. Requires `@EnableScheduling` (present on
 * [ai.cypherx.auth.AuthApplication]). A sweep failure is logged and swallowed so a transient DB blip
 * never kills the scheduler thread; the next tick retries.
 */
@Component
class SigningKeyRetirementJob(
    private val signingKeyService: SigningKeyService,
) {

    @Scheduled(
        fixedDelayString = "\${cypherx.auth.verifying-key-retirement-sweep-ms:3600000}",
        initialDelayString = "\${cypherx.auth.verifying-key-retirement-sweep-ms:3600000}",
    )
    fun sweep() {
        try {
            val retired = signingKeyService.retireExpiredVerifiers()
            if (retired > 0) {
                log.info("signing-key retirement sweep retired {} expired verifying key(s)", retired)
            } else {
                log.debug("signing-key retirement sweep: no keys eligible for retirement")
            }
        } catch (ex: Exception) {
            log.warn("signing-key retirement sweep failed (will retry next tick): {}", ex.message)
        }
    }

    private companion object {
        val log = LoggerFactory.getLogger(SigningKeyRetirementJob::class.java)
    }
}
