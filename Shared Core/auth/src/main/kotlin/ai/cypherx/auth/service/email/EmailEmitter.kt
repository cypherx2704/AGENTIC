package ai.cypherx.auth.service.email

/**
 * Pluggable transactional-email sender for the self-serve onboarding funnel (Component 1c).
 *
 * The only message the first-cycle funnel sends is the signup VERIFICATION email (a one-time link
 * carrying the opaque verification token). The concrete implementation is selected by env
 * (`ONBOARDING_EMAIL_PROVIDER=smtp|mock` ->
 * [ai.cypherx.auth.config.OnboardingProperties.emailProvider]): SMTP for prod (managed relay) and
 * local (mailhog), Mock for tests / no-SMTP local. Feature code depends only on this interface so
 * the provider swaps without touching [ai.cypherx.auth.service.OnboardingService].
 *
 * Sends are best-effort relative to the signup transaction: the signup row is already committed
 * (status `pending_verification`) before we send, so a transient email failure surfaces to the
 * caller as a clean error and the user can hit `POST /v1/onboarding/resend` — the durable state is
 * never lost. Implementations MUST NOT log the raw token or full link at INFO+ (secret hygiene).
 */
interface EmailEmitter {

    /**
     * Send a signup-verification email to [message].to carrying the verification link. Throws on a
     * hard send failure so the caller can map it to a Contract 2 error; the signup row persists
     * regardless (resend recovers).
     */
    fun sendVerification(message: VerificationEmail)

    /**
     * The verification email to send. [verificationUrl] embeds the raw token as a query parameter —
     * treat it as a secret (never log it). [tenantName] personalises the body.
     */
    data class VerificationEmail(
        val to: String,
        val tenantName: String,
        val verificationUrl: String,
    )
}
