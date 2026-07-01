package ai.cypherx.auth.service.captcha

import org.slf4j.LoggerFactory
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty
import org.springframework.stereotype.Component

/**
 * No-network [CaptchaVerifier] for tests and local/dev where no captcha provider is wired
 * (Component 1c). Selected when `ONBOARDING_CAPTCHA_PROVIDER` is `mock` (the default) — see
 * [ai.cypherx.auth.config.OnboardingProperties.captchaProvider].
 *
 * Behaviour (mirrors the other services' mock-provider convention): a present, non-blank token
 * PASSES; a missing/blank token FAILS — so happy-path tests pass any non-empty string and
 * negative tests omit the token. The sentinel token `"fail"` always FAILS, letting a test exercise
 * the 422 rejection path deterministically. The turnstile-backed implementation lands behind the
 * same interface when `ONBOARDING_CAPTCHA_PROVIDER=turnstile` (📋 post-first-cycle).
 */
@Component
@ConditionalOnProperty(
    prefix = "cypherx.auth.onboarding",
    name = ["captcha-provider"],
    havingValue = "mock",
    matchIfMissing = true,
)
class MockCaptchaVerifier : CaptchaVerifier {

    override fun verify(token: String?, remoteIp: String?): CaptchaVerifier.CaptchaResult {
        val clean = token?.trim().orEmpty()
        if (clean.isEmpty()) {
            log.debug("mock-captcha: missing token (ip={}) -> fail", remoteIp)
            return CaptchaVerifier.CaptchaResult(success = false, errorCode = "missing-input-response")
        }
        if (clean.equals(FAIL_SENTINEL, ignoreCase = true)) {
            log.debug("mock-captcha: fail-sentinel token (ip={}) -> fail", remoteIp)
            return CaptchaVerifier.CaptchaResult(success = false, errorCode = "invalid-input-response")
        }
        return CaptchaVerifier.CaptchaResult(success = true)
    }

    private companion object {
        val log = LoggerFactory.getLogger(MockCaptchaVerifier::class.java)

        /** A token equal to this (case-insensitive) always fails — for deterministic negative tests. */
        const val FAIL_SENTINEL = "fail"
    }
}
