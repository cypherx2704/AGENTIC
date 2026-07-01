package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.onboarding.*` configuration tree (WP04
 * Component 1c — self-serve onboarding). Bound by @ConfigurationPropertiesScan on
 * [ai.cypherx.auth.AuthApplication]. Every value is env-overridable — nothing here is a
 * hardcoded tunable; the in-code defaults are the documented fallbacks.
 *
 * Pluggable provider selection (the two providers swap by env, never by code edit):
 *  - [emailProvider]   `smtp` (prod / mailhog locally) | `mock` (tests / no-SMTP local). Picks the
 *    [ai.cypherx.auth.service.email.EmailEmitter] bean.
 *  - [captchaProvider] `turnstile` (prod) | `mock` (tests / local). Picks the
 *    [ai.cypherx.auth.service.captcha.CaptchaVerifier] bean.
 *
 * Risk / velocity scoring ([maxSignupsPerIpPerHour], [maxSignupsPerEmailPerDay],
 * [manualReviewRiskThreshold]) caps abuse on the public, unauthenticated signup surface
 * (rate-limit filter is the first line; this is the application-level second line that also
 * drives the `manual_review` hold).
 */
@ConfigurationProperties(prefix = "cypherx.auth.onboarding")
data class OnboardingProperties(

    /** Email provider: `smtp` (prod / mailhog) or `mock` (tests / local). Selects the EmailEmitter. */
    val emailProvider: String = "mock",

    /** Captcha provider: `turnstile` (prod) or `mock` (tests / local). Selects the CaptchaVerifier. */
    val captchaProvider: String = "mock",

    /** Verification-token lifetime (minutes) before a pending signup is `expired` (verify -> 410). */
    val verificationTtlMinutes: Long = 60 * 24,

    /** Default plan a self-serve tenant is created on (must exist in `auth.plan_defaults`). */
    val defaultPlan: String = "free",

    /** Default region for a self-serve tenant (mirrors auth.tenants default). */
    val defaultRegion: String = "us-east-1",

    /** Velocity cap: signups allowed per source IP per rolling hour (over the cap -> reject). */
    val maxSignupsPerIpPerHour: Int = 5,

    /** Velocity cap: signups allowed per email per rolling day (over the cap -> reject). */
    val maxSignupsPerEmailPerDay: Int = 3,

    /** Resend cap: max verification-email sends (signup + resends) per signup before refusal. */
    val maxResendAttempts: Int = 5,

    /**
     * Risk score in [0.0, 1.0] at/above which a signup is held for `manual_review` instead of
     * sending a verification email immediately. Velocity breaches and disposable-domain hits add
     * to the score (see [ai.cypherx.auth.service.OnboardingService]).
     */
    val manualReviewRiskThreshold: Double = 0.80,

    /** Public base URL used to build the verification link in the email body (Contract 1 iss host). */
    val verificationBaseUrl: String = "http://localhost:8080",

    /** SMTP transport config (used only when [emailProvider] == `smtp`). */
    val smtp: Smtp = Smtp(),
) {

    /**
     * SMTP transport settings for [ai.cypherx.auth.service.email.SmtpEmailEmitter]. Locally these
     * point at mailhog (`localhost:1025`, no auth, no TLS); in prod they point at the managed relay
     * (host/port/credentials supplied by env). [username]/[password] blank => unauthenticated send.
     */
    data class Smtp(
        val host: String = "localhost",
        val port: Int = 1025,
        val username: String = "",
        val password: String = "",
        val startTls: Boolean = false,
        /** From-address on the verification email (e.g. no-reply@cypherx.ai). */
        val from: String = "no-reply@cypherx.local",
        /** Connection / read timeout (ms) so a wedged relay never stalls the signup request. */
        val timeoutMs: Int = 5000,
    )
}
