package ai.cypherx.auth.api

import ai.cypherx.auth.repo.Tenant
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.OnboardingService
import ai.cypherx.auth.service.SignupCommand
import ai.cypherx.auth.web.ApiException
import jakarta.servlet.http.HttpServletRequest
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import java.time.format.DateTimeFormatter

/**
 * Self-serve onboarding HTTP surface (WP04 Component 1c — amended).
 *
 *  PUBLIC (body/token-authenticated; permit-all in [ai.cypherx.auth.config.SecurityConfig]):
 *    POST /v1/onboarding/signup    captcha + risk-scored signup -> verification email   202
 *    GET  /v1/onboarding/verify    consume token -> provision tenant/agent/key          200 / 410
 *    POST /v1/onboarding/resend    rotate token + re-email (anti-enumeration)            202
 *
 *  AUTHENTICATED (tenant-admin JWT; resolved from the bearer via [CallerContext]):
 *    POST /v1/onboarding/upgrade   plan-change request for the caller's tenant           202
 *    POST /v1/onboarding/close     tenant-close (soft-delete) request                    202
 *
 * Errors are thrown as [ApiException] and rendered by the Core GlobalExceptionHandler as the
 * Contract 2 envelope — this controller never hand-builds error bodies. Scope enforcement for the
 * authenticated routes is done in-controller (method security is not enabled service-wide), matching
 * [TenantAdminController].
 */
@RestController
@RequestMapping("/v1/onboarding")
class OnboardingController(
    private val onboardingService: OnboardingService,
    private val callerContext: CallerContext,
) {

    // ── Public ───────────────────────────────────────────────────────────────────────────

    @PostMapping("/signup")
    fun signup(
        @RequestBody(required = false) body: SignupRequest?,
        request: HttpServletRequest,
    ): ResponseEntity<Map<String, Any?>> {
        val req = body ?: SignupRequest()
        val result = onboardingService.signup(
            SignupCommand(
                email = req.email,
                tenantName = req.tenantName,
                captchaToken = req.captchaToken,
                ipAddress = clientIp(request),
                userAgent = request.getHeader("User-Agent"),
            ),
        )
        // 202: the verification email is queued; the tenant is NOT created until verify.
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(
            linkedMapOf(
                "signup_id" to result.signupId.toString(),
                "status" to result.status,
                "expires_at" to TIMESTAMP_FMT.format(result.expiresAt),
                "message" to "If the details are valid, a verification email has been sent.",
            ),
        )
    }

    @GetMapping("/verify")
    fun verify(@RequestParam(name = "token", required = false) token: String?): Map<String, Any?> {
        val result = onboardingService.verify(token)
        // The raw api key is returned ONCE here and is unrecoverable thereafter.
        return linkedMapOf(
            "tenant_id" to result.tenantId.toString(),
            "tenant_name" to result.tenantName,
            "plan" to result.plan,
            "agent_id" to result.agentId.toString(),
            "api_key_id" to result.apiKeyId.toString(),
            "api_key" to result.apiKey,
            "key_prefix" to result.keyPrefix,
            "next" to "Mint a JWT via POST /v1/agents/${result.agentId}/token using this api key.",
        )
    }

    @PostMapping("/resend")
    fun resend(@RequestBody(required = false) body: ResendRequest?): ResponseEntity<Map<String, Any?>> {
        onboardingService.resend(body?.email)
        // Always the same 202 (anti-enumeration) — never reveals whether the email is known/pending.
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(
            linkedMapOf("message" to "If a pending signup exists for that email, a new verification email has been sent."),
        )
    }

    // ── Authenticated (tenant-admin) ───────────────────────────────────────────────────────

    @PostMapping("/upgrade")
    fun upgrade(@RequestBody(required = false) body: UpgradeRequest?): ResponseEntity<Map<String, Any?>> {
        requireScope(SCOPE_TENANT_ADMIN)
        val tenantId = callerContext.current().tenantId
        val tenant = onboardingService.requestUpgrade(tenantId, body?.newPlan)
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(planView(tenant, "Plan change applied."))
    }

    @PostMapping("/close")
    fun close(): ResponseEntity<Map<String, Any?>> {
        requireScope(SCOPE_TENANT_ADMIN)
        val tenantId = callerContext.current().tenantId
        val tenant = onboardingService.requestClose(tenantId)
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(
            linkedMapOf(
                "tenant_id" to tenant.tenantId.toString(),
                "status" to tenant.status.value,
                "pending_deletion_at" to tenant.pendingDeletionAt?.let(TIMESTAMP_FMT::format),
                "message" to "Tenant scheduled for closure; a grace window applies before permanent deletion.",
            ),
        )
    }

    // ── Helpers ──────────────────────────────────────────────────────────────────────────

    private fun planView(tenant: Tenant, message: String): Map<String, Any?> = linkedMapOf(
        "tenant_id" to tenant.tenantId.toString(),
        "plan" to tenant.plan,
        "status" to tenant.status.value,
        "message" to message,
    )

    /**
     * Programmatic scope guard (method security is not enabled in the locked SecurityConfig). The
     * Core [ai.cypherx.auth.web.AgentJwtAuthFilter] sets authorities as `SCOPE_<scope>`; a
     * `platform:admin` token satisfies any required scope. Missing scope -> Contract 2 403.
     */
    private fun requireScope(scope: String) {
        val authorities = SecurityContextHolder.getContext().authentication
            ?.authorities
            ?.map { it.authority }
            ?.toSet()
            ?: emptySet()
        if ("SCOPE_$scope" in authorities || "SCOPE_$SCOPE_PLATFORM_ADMIN" in authorities) return
        throw ApiException.forbidden("Caller lacks required scope: $scope", mapOf("required" to scope))
    }

    /** Best-effort client IP: X-Forwarded-For first hop, else remote addr (matches AuthorizeController). */
    private fun clientIp(request: HttpServletRequest): String? =
        request.getHeader("X-Forwarded-For")
            ?.split(",")
            ?.firstOrNull()
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
            ?: request.remoteAddr

    // ── Inbound bodies ─────────────────────────────────────────────────────────────────────

    /** `POST /v1/onboarding/signup` body. `captcha_token` is the provider challenge response. */
    data class SignupRequest(
        val email: String? = null,
        val tenantName: String? = null,
        val captchaToken: String? = null,
    )

    /** `POST /v1/onboarding/resend` body. */
    data class ResendRequest(val email: String? = null)

    /** `POST /v1/onboarding/upgrade` body. */
    data class UpgradeRequest(val newPlan: String? = null)

    private companion object {
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
        const val SCOPE_TENANT_ADMIN = "tenant:admin"
        val TIMESTAMP_FMT: DateTimeFormatter = DateTimeFormatter.ISO_INSTANT
    }
}
