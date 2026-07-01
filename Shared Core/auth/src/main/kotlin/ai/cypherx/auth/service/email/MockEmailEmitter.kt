package ai.cypherx.auth.service.email

import org.slf4j.LoggerFactory
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty
import org.springframework.stereotype.Component
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * No-network [EmailEmitter] for tests and local/dev where no SMTP relay is wired (Component 1c).
 * Selected when `ONBOARDING_EMAIL_PROVIDER` is `mock` (the default) — see
 * [ai.cypherx.auth.config.OnboardingProperties.emailProvider].
 *
 * Instead of sending, it records each message in an in-memory [sent] queue so an integration test
 * can assert the verification email was "sent" and read back the link to drive the verify step
 * (the SMTP/mailhog path does the same out-of-band locally). Bounded so a long-running local
 * process cannot leak memory. It logs only the recipient + tenant name at INFO — NEVER the link
 * (which carries the raw token).
 */
@Component
@ConditionalOnProperty(
    prefix = "cypherx.auth.onboarding",
    name = ["email-provider"],
    havingValue = "mock",
    matchIfMissing = true,
)
class MockEmailEmitter : EmailEmitter {

    /** Most-recently-"sent" verification emails (bounded), exposed for tests / local inspection. */
    val sent: ConcurrentLinkedQueue<EmailEmitter.VerificationEmail> = ConcurrentLinkedQueue()

    override fun sendVerification(message: EmailEmitter.VerificationEmail) {
        sent.add(message)
        // Bound the buffer (drop oldest) so a local long-run cannot grow without limit.
        while (sent.size > MAX_BUFFERED) sent.poll()
        // Secret hygiene: log recipient + tenant, NEVER the verification URL (carries the raw token).
        log.info("mock-email: queued verification email to {} for tenant '{}'", message.to, message.tenantName)
    }

    /** Convenience for tests: the most recent message to [recipient], or null. */
    fun lastFor(recipient: String): EmailEmitter.VerificationEmail? =
        sent.toList().lastOrNull { it.to.equals(recipient, ignoreCase = true) }

    private companion object {
        val log = LoggerFactory.getLogger(MockEmailEmitter::class.java)
        const val MAX_BUFFERED = 256
    }
}
