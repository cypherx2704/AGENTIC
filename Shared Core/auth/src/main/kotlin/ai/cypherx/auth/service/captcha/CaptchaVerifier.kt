package ai.cypherx.auth.service.captcha

/**
 * Pluggable captcha verification for the public self-serve signup endpoint (Component 1c).
 *
 * The public `POST /v1/onboarding/signup` is unauthenticated, so a captcha (Cloudflare Turnstile
 * in prod) is the human-presence gate in front of tenant provisioning. The concrete implementation
 * is selected by env (`ONBOARDING_CAPTCHA_PROVIDER=mock|turnstile` ->
 * [ai.cypherx.auth.config.OnboardingProperties.captchaProvider]); feature code depends only on this
 * interface so the provider swaps without touching the onboarding flow.
 *
 * Implementations MUST be side-effect-free beyond the upstream verify call and MUST NOT throw on a
 * failed challenge — they return [CaptchaResult] and let the service map a failure to a Contract 2
 * 422 (so a captcha-provider outage is a clean validation error, never a 500).
 */
interface CaptchaVerifier {

    /**
     * Verify a captcha challenge [token] presented at signup, optionally binding it to the client
     * [remoteIp] (Turnstile echoes/checks the IP). Returns the structured [CaptchaResult]; never
     * throws for an ordinary "challenge failed" — only genuinely exceptional transport faults
     * (which the implementation may surface as `success = false`).
     */
    fun verify(token: String?, remoteIp: String?): CaptchaResult

    /** Outcome of a captcha verification. [success] gates the signup; [errorCode] aids diagnostics. */
    data class CaptchaResult(
        val success: Boolean,
        val errorCode: String? = null,
    )
}
