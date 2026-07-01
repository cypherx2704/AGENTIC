package ai.cypherx.auth.service

import ai.cypherx.auth.config.OnboardingProperties
import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.AgentType
import ai.cypherx.auth.domain.SYSTEM_USER_ID
import ai.cypherx.auth.domain.TenantSource
import ai.cypherx.auth.kafka.OutboxEventWriter
import ai.cypherx.auth.repo.SignupAttemptRepository
import ai.cypherx.auth.repo.Tenant
import ai.cypherx.auth.repo.TenantRepository
import ai.cypherx.auth.service.captcha.CaptchaVerifier
import ai.cypherx.auth.service.email.EmailEmitter
import ai.cypherx.auth.web.ApiException
import org.slf4j.LoggerFactory
import org.springframework.dao.DuplicateKeyException
import org.springframework.http.HttpStatus
import org.springframework.stereotype.Service
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import java.time.Duration
import java.time.Instant
import java.util.Base64
import java.util.UUID
import java.util.regex.Pattern

/**
 * Self-serve onboarding orchestration (WP04 Component 1c — amended). Owns the unauthenticated funnel
 * that lets a prospect create a tenant without an existing admin:
 *
 *   signup  -> captcha verify + velocity/risk scoring -> persist a pending `signup_attempt`
 *              -> email a one-time verification link (202).
 *   verify  -> validate the token (hash match + not expired + still pending) -> provision the
 *              tenant + first agent + first api_key (raw key shown ONCE) -> emit
 *              `cypherx.tenant.created` via the transactional outbox (200; 410 if expired/consumed).
 *   resend  -> rotate the token + re-email (202).
 *   upgrade -> record a plan-change request for the caller's tenant (202).
 *   close   -> record a tenant-close request for the caller's tenant (202).
 *
 * Security posture: signup/verify/resend are PUBLIC (no JWT). The verification token is the
 * bearer-of-record there — we store only its SHA-256 hash (never the raw token), mirroring how
 * `api_keys` stores `key_hash`. Provisioning is provenance-honest: the tenant is created with source
 * `self-serve-signup` (Contract 13) and its `cypherx.tenant.created` event commits in the SAME
 * transaction as the tenant row (publication guarantee — no log-and-drop). upgrade/close run under a
 * tenant-admin JWT (the controller enforces the scope).
 *
 * Pluggable providers ([emailEmitter], [captchaVerifier]) are chosen by env at bean-selection time
 * (see [OnboardingProperties]); this service depends only on the interfaces.
 */
@Service
class OnboardingService(
    private val signupAttempts: SignupAttemptRepository,
    private val tenantRepository: TenantRepository,
    private val agentService: AgentService,
    private val apiKeyService: ApiKeyService,
    private val tenantService: TenantService,
    private val outboxEvents: OutboxEventWriter,
    private val tenantTx: TenantTx,
    private val emailEmitter: EmailEmitter,
    private val captchaVerifier: CaptchaVerifier,
    private val auditService: AuditService,
    private val props: OnboardingProperties,
) {

    private val rng = SecureRandom()

    // ── 1. Signup (public) ────────────────────────────────────────────────────────────────

    /**
     * Begin a self-serve signup: verify the captcha, score velocity/risk, persist a pending
     * `signup_attempt`, and email a one-time verification link. Returns the [SignupResult] the
     * controller renders as 202 Accepted. We DELIBERATELY return the same 202 for "queued" and
     * "held for manual review" so the endpoint never reveals whether an email is already known
     * (anti-enumeration); only a captcha failure / blatant velocity breach is surfaced.
     */
    fun signup(cmd: SignupCommand): SignupResult {
        val email = normalizeEmail(cmd.email)
        val tenantName = cmd.tenantName?.trim()?.takeIf { it.isNotEmpty() }
            ?: throw ApiException.validation("tenant_name is required", mapOf("field" to "tenant_name"))

        // 1) Captcha gate (human-presence on the public surface). Provider chosen by env.
        val captcha = captchaVerifier.verify(cmd.captchaToken, cmd.ipAddress)
        if (!captcha.success) {
            throw ApiException.validation(
                "Captcha verification failed",
                mapOf("field" to "captcha_token", "error" to captcha.errorCode),
            )
        }

        // 2) Velocity / risk scoring (application-level abuse second line; the rate-limit filter is
        //    the first). A hard breach rejects; a softer score holds the attempt for manual review.
        val score = scoreRisk(email, cmd.ipAddress)
        if (score.hardReject) {
            throw ApiException(
                "SIGNUP_VELOCITY_EXCEEDED",
                HttpStatus.TOO_MANY_REQUESTS,
                "Too many signups from this source; please try again later",
                mapOf("retry_after_seconds" to 3600),
            )
        }
        val status = if (score.value >= props.manualReviewRiskThreshold) STATUS_MANUAL_REVIEW else STATUS_PENDING

        // 3) Persist the pending attempt with ONLY the token hash; raw token is emailed then dropped.
        val rawToken = generateToken()
        val tokenHash = sha256Hex(rawToken)
        val expiresAt = Instant.now().plus(Duration.ofMinutes(props.verificationTtlMinutes))
        val attempt = signupAttempts.insert(
            email = email,
            tenantName = tenantName,
            verificationTokenHash = tokenHash,
            verificationExpiresAt = expiresAt,
            riskScore = score.value,
            status = status,
            ipAddress = cmd.ipAddress,
            userAgent = cmd.userAgent,
        )

        // 4) Held attempts are NOT emailed (a human reviews them first). Otherwise email the link.
        if (status == STATUS_PENDING) {
            sendVerificationEmail(email, tenantName, rawToken)
        } else {
            log.info("signup {} held for manual review (risk={})", attempt.signupId, score.value)
        }

        return SignupResult(signupId = attempt.signupId, status = status, expiresAt = expiresAt)
    }

    // ── 2. Verify (public) ────────────────────────────────────────────────────────────────

    /**
     * Consume a verification token: provision the tenant + first agent + first api_key and emit
     * `cypherx.tenant.created`. Idempotency/abuse safety: a two-phase single-winner claim
     * (`pending_verification -> verifying`, then `verifying -> verified` after provisioning) means a
     * replayed/double-clicked link can neither create a second tenant nor leave an orphan tenant —
     * only the claim winner provisions. An unknown / already-consumed / in-flight token yields 410
     * Gone; a stale-but-pending row is lazily flipped to `expired`.
     */
    fun verify(rawToken: String?): VerifyResult {
        val token = rawToken?.trim().orEmpty()
        if (token.isEmpty()) {
            throw ApiException.validation("token is required", mapOf("field" to "token"))
        }
        val attempt = signupAttempts.findByTokenHash(sha256Hex(token))
            ?: throw ApiException.gone("Verification link is invalid or has already been used")

        // IDEMPOTENT REPLAY (Component 1c resilience). A token whose tenant was ALREADY provisioned is
        // being re-verified — typically because the client never received the original response (a BFF
        // upstream timeout / dropped connection AFTER Auth committed the tenant+agent+key). Returning 410
        // here would strand the tenant and permanently lose the one-time key. Instead, re-derive the
        // result from the existing tenant and mint a FRESH initial key, so simply re-opening the link (or
        // an automatic client retry) recovers a working credential. Bounded to the link's validity window:
        // once the token has expired, recovery is closed and the user must sign up again.
        if (attempt.status == STATUS_VERIFIED && attempt.tenantId != null) {
            if (Instant.now().isAfter(attempt.verificationExpiresAt)) {
                throw ApiException.gone("Verification link has expired; please sign up again")
            }
            return recoverProvisionedTenant(attempt.tenantId)
        }
        if (attempt.status != STATUS_PENDING) {
            // verifying (claimed mid-flight) / manual_review / rejected / expired — not link-consumable.
            throw ApiException.gone("Verification link is no longer valid")
        }
        if (Instant.now().isAfter(attempt.verificationExpiresAt)) {
            signupAttempts.markExpired(attempt.signupId)
            throw ApiException.gone("Verification link has expired; request a new one")
        }

        val tenantName = attempt.tenantName?.takeIf { it.isNotBlank() } ?: attempt.email

        // Claim the attempt FIRST (single-winner: pending -> verifying). Only the winner provisions,
        // so a concurrent double-click cannot create a second tenant or an orphan tenant.
        if (!signupAttempts.claimForVerification(attempt.signupId)) {
            throw ApiException.gone("Verification link has already been used")
        }

        // Provision the tenant + the durable cypherx.tenant.created event in ONE transaction
        // (publication guarantee — mirrors TenantService.create but with self-serve provenance).
        val tenant = provisionTenant(tenantName)

        // Record the provisioned tenant and finalise the attempt (verifying -> verified).
        signupAttempts.attachProvisionedTenant(attempt.signupId, tenant.tenantId, initialAdminUserId = null)

        // Seed the initial effective-quota row from plan defaults (best-effort; re-derivable).
        runCatching {
            val limits = tenantRepository.planDefaultLimits(tenant.plan)
            if (limits != null) {
                tenantRepository.seedQuotasFromPlan(
                    tenant.tenantId, tenant.plan, limits, updatedBy = SYSTEM_USER_ID.toString(),
                )
            }
        }.onFailure { log.warn("quota seed failed for self-serve tenant {}: {}", tenant.tenantId, it.message) }

        // First agent + first api_key (raw key shown ONCE). A synthetic platform-admin caller lets
        // us target the freshly-created tenant through the existing, un-edited services.
        val systemCaller = AgentService.Caller(
            agentId = SYSTEM_USER_ID,
            tenantId = tenant.tenantId,
            scopes = setOf(SCOPE_PLATFORM_ADMIN),
        )
        val agent = agentService.createAgent(
            AgentService.CreateAgentCommand(
                name = FIRST_AGENT_NAME,
                version = FIRST_AGENT_VERSION,
                allowedScopes = FIRST_AGENT_SCOPES,
                requestedTenantId = tenant.tenantId,
                agentType = AgentType.ORCHESTRATOR,
            ),
            systemCaller,
        )
        val key = apiKeyService.issue(
            tenantId = tenant.tenantId,
            agentId = agent.agentId,
            scopes = FIRST_AGENT_SCOPES,
            name = "onboarding-initial-key",
            expiresInDays = null,
        )

        // Durable audit (best-effort; never blocks the response).
        runCatching {
            auditService.record(
                eventType = "tenant.created",
                tenantId = tenant.tenantId,
                agentId = agent.agentId,
                action = "onboarding:verify",
                resource = "tenant:${tenant.tenantId}",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for onboarding verify {}: {}", tenant.tenantId, it.message) }

        log.info("onboarding verified — provisioned tenant {} + first agent {}", tenant.tenantId, agent.agentId)

        return VerifyResult(
            tenantId = tenant.tenantId,
            tenantName = tenant.name,
            plan = tenant.plan,
            agentId = agent.agentId,
            apiKeyId = key.keyId,
            apiKey = key.rawKey,
            keyPrefix = key.keyPrefix,
        )
    }

    /**
     * Idempotent-replay recovery (see [verify]). The tenant for [tenantId] was already provisioned by a
     * prior verify whose response the client never received. Ensure the first agent exists (re-create
     * defensively if a mid-flight failure left the tenant without one), mint a FRESH initial key —
     * revoking any prior active keys first so the per-agent key quota is never exceeded across repeated
     * recoveries — and return the same [VerifyResult] shape. Idempotent: never creates a second tenant,
     * and the active-key count stays at one no matter how many times the link is replayed.
     */
    private fun recoverProvisionedTenant(tenantId: UUID): VerifyResult {
        val tenant = tenantService.get(tenantId)
        val systemCaller = AgentService.Caller(
            agentId = SYSTEM_USER_ID,
            tenantId = tenantId,
            scopes = setOf(SCOPE_PLATFORM_ADMIN),
        )
        // Find the onboarding default agent; re-create it only if a partial earlier failure left none.
        val agent = agentService.listAgents(
            caller = systemCaller,
            statusFilter = null,
            nameContains = FIRST_AGENT_NAME,
            cursor = null,
            limit = 50,
        ).agents.firstOrNull { it.name == FIRST_AGENT_NAME }
            ?: agentService.createAgent(
                AgentService.CreateAgentCommand(
                    name = FIRST_AGENT_NAME,
                    version = FIRST_AGENT_VERSION,
                    allowedScopes = FIRST_AGENT_SCOPES,
                    requestedTenantId = tenantId,
                    agentType = AgentType.ORCHESTRATOR,
                ),
                systemCaller,
            )

        // Re-mint: revoke prior active keys (keeps the active-key count bounded under the quota) then
        // issue exactly one fresh key. Revoke is best-effort — a hiccup here must not block recovery.
        runCatching { apiKeyService.revokeAllForAgent(tenantId, agent.agentId) }
            .onFailure { log.warn("recovery: revoke prior keys failed for agent {}: {}", agent.agentId, it.message) }
        val key = apiKeyService.issue(
            tenantId = tenantId,
            agentId = agent.agentId,
            scopes = FIRST_AGENT_SCOPES,
            name = "onboarding-recovery-key",
            expiresInDays = null,
        )

        log.info("onboarding replay recovered — tenant {} + agent {} (fresh key minted)", tenantId, agent.agentId)
        return VerifyResult(
            tenantId = tenantId,
            tenantName = tenant.name,
            plan = tenant.plan,
            agentId = agent.agentId,
            apiKeyId = key.keyId,
            apiKey = key.rawKey,
            keyPrefix = key.keyPrefix,
        )
    }

    // ── 3. Resend (public) ────────────────────────────────────────────────────────────────

    /**
     * Re-issue a verification email for a still-pending signup (rotates the token). Anti-enumeration:
     * an unknown / already-verified / over-cap email returns the SAME 202 as success without sending,
     * so the endpoint never confirms which emails exist. A genuinely pending row gets a fresh token.
     */
    fun resend(emailRaw: String?) {
        val email = normalizeEmail(emailRaw)
        val latest = signupAttempts.findLatestByEmail(email)
        if (latest == null || latest.status != STATUS_PENDING || latest.attempts >= props.maxResendAttempts) {
            // Silent no-op (same response shape) — do not reveal account state.
            log.debug("resend no-op for {} (state-hidden)", email)
            return
        }
        val rawToken = generateToken()
        val expiresAt = Instant.now().plus(Duration.ofMinutes(props.verificationTtlMinutes))
        val rotated = signupAttempts.rotateTokenForResend(latest.signupId, sha256Hex(rawToken), expiresAt)
        if (rotated != null) {
            sendVerificationEmail(email, rotated.tenantName ?: email, rawToken)
        }
    }

    // ── 4. Upgrade request (authenticated: tenant-admin) ──────────────────────────────────

    /**
     * Record a plan-change (upgrade/downgrade) request for the caller's own [tenantId]. Validated +
     * applied through the existing [TenantService.changePlan] (which emits
     * `cypherx.tenant.plan_changed` and re-seeds quotas downstream). The caller's tenant is resolved
     * by the controller from the JWT — never trusted from the body.
     */
    fun requestUpgrade(tenantId: UUID, newPlan: String?): Tenant {
        val plan = newPlan?.trim()?.takeIf { it.isNotEmpty() }
            ?: throw ApiException.validation("new_plan is required", mapOf("field" to "new_plan"))
        return tenantService.changePlan(tenantId, plan, source = "self-serve-upgrade")
    }

    // ── 5. Close request (authenticated: tenant-admin) ────────────────────────────────────

    /**
     * Record a tenant-close request for the caller's own [tenantId]: soft-delete (30-day grace,
     * emits `cypherx.tenant.pending_deletion`) via the existing [TenantService.softDelete]. The
     * tenant is resolved by the controller from the JWT.
     */
    fun requestClose(tenantId: UUID): Tenant = tenantService.softDelete(tenantId)

    // ── Internal: provisioning ────────────────────────────────────────────────────────────

    /**
     * Create the self-serve tenant + its `cypherx.tenant.created` outbox row in ONE platform
     * transaction (the publication guarantee). Source is `self-serve-signup` (Contract 13) so audit
     * provenance is honest — we cannot route this through [TenantService.create], which rejects any
     * source other than manual-seed/external-admin, so we replicate its create+emit pattern here
     * against the same un-edited repository + outbox writer.
     */
    private fun provisionTenant(name: String): Tenant {
        val plan = props.defaultPlan
        val region = props.defaultRegion
        // Validate the plan exists up front (clean 422 rather than a deferred quota-seed failure).
        tenantRepository.planDefaultLimits(plan)
            ?: throw ApiException.validation("Onboarding default plan '$plan' is not configured", mapOf("plan" to plan))

        val tenantId = UUID.randomUUID()
        return try {
            tenantTx.inPlatform { jdbc ->
                val created = tenantRepository.insert(
                    tenantId = tenantId,
                    name = name,
                    plan = plan,
                    source = TenantSource.SELF_SERVE_SIGNUP,
                    sourceMetadataJson = "{}",
                    region = region,
                )
                outboxEvents.tenantCreated(
                    jdbc,
                    tenantId = tenantId,
                    plan = plan,
                    source = TenantSource.SELF_SERVE_SIGNUP.value,
                    region = region,
                    createdAt = created.createdAt,
                )
                created
            }
        } catch (ex: DuplicateKeyException) {
            // Astronomically unlikely (random UUID); surface as a clean conflict rather than a 500.
            throw ApiException.conflict("Tenant already exists", mapOf("tenant_id" to tenantId.toString()))
        }
    }

    /** Build the verification link and hand it to the env-selected emitter. */
    private fun sendVerificationEmail(email: String, tenantName: String, rawToken: String) {
        val base = props.verificationBaseUrl.trimEnd('/')
        val url = "$base/v1/onboarding/verify?token=" + URLEncoder.encode(rawToken, StandardCharsets.UTF_8)
        emailEmitter.sendVerification(
            EmailEmitter.VerificationEmail(to = email, tenantName = tenantName, verificationUrl = url),
        )
    }

    // ── Internal: risk scoring ────────────────────────────────────────────────────────────

    /**
     * Velocity/risk score in [0.0, 1.0] from recent attempts by the same IP (last hour) and email
     * (last day), plus a disposable-domain signal. [RiskScore.hardReject] fires when a velocity cap
     * is blown outright; otherwise the score may cross [OnboardingProperties.manualReviewRiskThreshold]
     * and hold the attempt for review. Scoring NEVER blocks on a DB hiccup — it fails open (score 0).
     */
    private fun scoreRisk(email: String, ip: String?): RiskScore {
        val now = Instant.now()
        var score = 0.0
        var hardReject = false

        if (!ip.isNullOrBlank()) {
            val ipCount = runCatching { signupAttempts.countByIpSince(ip, now.minus(Duration.ofHours(1))) }
                .getOrDefault(0L)
            if (ipCount >= props.maxSignupsPerIpPerHour) hardReject = true
            score += (ipCount.toDouble() / props.maxSignupsPerIpPerHour.coerceAtLeast(1)).coerceAtMost(0.6)
        }

        val emailCount = runCatching { signupAttempts.countByEmailSince(email, now.minus(Duration.ofDays(1))) }
            .getOrDefault(0L)
        if (emailCount >= props.maxSignupsPerEmailPerDay) hardReject = true
        score += (emailCount.toDouble() / props.maxSignupsPerEmailPerDay.coerceAtLeast(1)).coerceAtMost(0.3)

        if (isDisposableDomain(email)) score += 0.4

        return RiskScore(value = score.coerceIn(0.0, 1.0), hardReject = hardReject)
    }

    private fun isDisposableDomain(email: String): Boolean {
        val domain = email.substringAfter('@', "").lowercase()
        return domain.isNotEmpty() && DISPOSABLE_DOMAINS.contains(domain)
    }

    // ── Internal: helpers ─────────────────────────────────────────────────────────────────

    private fun normalizeEmail(raw: String?): String {
        val email = raw?.trim()?.lowercase().orEmpty()
        if (email.isEmpty() || !EMAIL_PATTERN.matcher(email).matches()) {
            throw ApiException.validation("A valid email is required", mapOf("field" to "email"))
        }
        return email
    }

    /** 256-bit URL-safe opaque token. Only its SHA-256 hash is persisted. */
    private fun generateToken(): String {
        val bytes = ByteArray(TOKEN_BYTES)
        rng.nextBytes(bytes)
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    private fun sha256Hex(raw: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(raw.toByteArray(StandardCharsets.UTF_8))
        return digest.joinToString("") { "%02x".format(it) }
    }

    private data class RiskScore(val value: Double, val hardReject: Boolean)

    private companion object {
        val log = LoggerFactory.getLogger(OnboardingService::class.java)

        const val STATUS_PENDING = "pending_verification"
        const val STATUS_VERIFIED = "verified"
        const val STATUS_MANUAL_REVIEW = "manual_review"

        const val SCOPE_PLATFORM_ADMIN = "platform:admin"

        const val TOKEN_BYTES = 32

        const val FIRST_AGENT_NAME = "orchestrator"
        const val FIRST_AGENT_VERSION = "1.0.0"

        /**
         * Scopes granted to a self-serve tenant's FIRST agent — its mandatory ORCHESTRATOR. Shared
         * with [UserAuthService] via [ai.cypherx.auth.domain.ORCHESTRATOR_DEFAULT_SCOPES] so the
         * auto-provisioned orchestrator is identical regardless of signup entry point.
         */
        val FIRST_AGENT_SCOPES = ai.cypherx.auth.domain.ORCHESTRATOR_DEFAULT_SCOPES

        val EMAIL_PATTERN: Pattern = Pattern.compile("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")

        /** Minimal disposable-domain denylist (env-extensible later); a soft risk signal, not a block. */
        val DISPOSABLE_DOMAINS = setOf(
            "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "yopmail.com",
        )
    }
}

// ── Service-level DTOs ──────────────────────────────────────────────────────────────────────

/** Inbound, shape-checked at the controller. [ipAddress]/[userAgent] are derived from the request. */
data class SignupCommand(
    val email: String?,
    val tenantName: String?,
    val captchaToken: String?,
    val ipAddress: String?,
    val userAgent: String?,
)

/** Result of [OnboardingService.signup] — rendered as 202. No secret is exposed here. */
data class SignupResult(
    val signupId: UUID,
    val status: String,
    val expiresAt: Instant,
)

/** Result of [OnboardingService.verify] — the ONLY time the initial raw api key is exposed. */
data class VerifyResult(
    val tenantId: UUID,
    val tenantName: String,
    val plan: String,
    val agentId: UUID,
    val apiKeyId: UUID,
    val apiKey: String,
    val keyPrefix: String,
)
