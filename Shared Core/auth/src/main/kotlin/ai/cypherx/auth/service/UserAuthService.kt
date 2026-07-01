package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.config.GoogleOAuthProperties
import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.AgentType
import ai.cypherx.auth.domain.LoginProvider
import ai.cypherx.auth.domain.ORCHESTRATOR_DEFAULT_SCOPES
import ai.cypherx.auth.domain.SYSTEM_USER_ID
import ai.cypherx.auth.domain.TenantSource
import ai.cypherx.auth.kafka.OutboxEventWriter
import ai.cypherx.auth.repo.AgentRecord
import ai.cypherx.auth.repo.AgentRepository
import ai.cypherx.auth.repo.Tenant
import ai.cypherx.auth.repo.TenantRepository
import ai.cypherx.auth.repo.UserRecord
import ai.cypherx.auth.repo.UserRepository
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.ObjectMapper
import de.mkammerer.argon2.Argon2Factory
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.http.HttpStatus
import org.springframework.stereotype.Service
import java.net.URI
import java.net.URLEncoder
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.security.SecureRandom
import java.time.Duration
import java.time.Instant
import java.util.Base64
import java.util.UUID
import java.util.regex.Pattern

/**
 * End-user authentication (email/password + "Sign in with Gmail" / Google OAuth2). This is the human
 * login layer that sits IN FRONT of the agent-identity system: a successful login resolves the user's
 * tenant and mints an agent JWT for that tenant's mandatory ORCHESTRATOR agent (the identity the
 * Console operates as). Agents/api-keys still exist for SDK/programmatic use.
 *
 * Registration provisions, in order: tenant (source self-serve-signup) + its `cypherx.tenant.created`
 * outbox event (publication guarantee, one tx) -> default-plan quota seed -> the `auth.users` row ->
 * the single ORCHESTRATOR agent (via [AgentService], which the `uq_orchestrator_per_tenant` index
 * keeps unique) -> an initial api_key (raw secret returned ONCE).
 *
 * Passwords are Argon2id (same parameters as [OAuthService] client secrets). `auth.users` is
 * platform-scoped (login resolves a user before any tenant context exists).
 */
@Service
class UserAuthService(
    private val userRepository: UserRepository,
    private val agentRepository: AgentRepository,
    private val agentService: AgentService,
    private val apiKeyService: ApiKeyService,
    private val tenantRepository: TenantRepository,
    private val outboxEvents: OutboxEventWriter,
    private val tenantTx: TenantTx,
    private val jwtMintService: JwtMintService,
    private val auditService: AuditService,
    private val objectMapper: ObjectMapper,
    private val props: AuthProperties,
    private val googleProps: GoogleOAuthProperties,
) {

    /** Optional — jti tracking degrades gracefully when Valkey is absent (also backs Google state). */
    @Autowired(required = false)
    private var redis: StringRedisTemplate? = null

    private val argon2 = Argon2Factory.create(Argon2Factory.Argon2Types.ARGON2id)
    private val rng = SecureRandom()
    private val httpClient: HttpClient = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(10)).build()

    // ── DTOs ────────────────────────────────────────────────────────────────────────────────

    data class RegisterResult(
        val userId: UUID,
        val tenantId: UUID,
        val orchestratorAgentId: UUID,
        val apiKeyId: UUID,
        val apiKey: String,
        val keyPrefix: String,
    )

    /** What the BFF stores in the session (token) + echoes to the SPA (everything but the token). */
    data class LoginResult(
        val userId: UUID,
        val tenantId: UUID,
        val agentId: UUID,
        val token: String,
        val expiresIn: Long,
        val scopes: List<String>,
    )

    data class GoogleAuthUrl(val url: String, val state: String)

    // ── 1. Register (email + password) ────────────────────────────────────────────────────

    fun register(emailRaw: String?, password: String?, tenantNameRaw: String?, displayName: String?): RegisterResult {
        val email = normalizeEmail(emailRaw)
        val pwd = password?.takeIf { it.isNotBlank() }
            ?: throw ApiException.validation("password is required", mapOf("field" to "password"))
        if (pwd.length < MIN_PASSWORD_LEN) {
            throw ApiException.validation(
                "password must be at least $MIN_PASSWORD_LEN characters",
                mapOf("field" to "password"),
            )
        }
        if (userRepository.findByEmail(email) != null) {
            throw ApiException.conflict("An account with this email already exists", mapOf("email" to email))
        }
        val tenantName = tenantNameRaw?.trim()?.takeIf { it.isNotEmpty() } ?: email.substringBefore('@')

        val tenant = provisionTenant(tenantName)
        seedQuotas(tenant)
        val user = userRepository.insert(
            tenantId = tenant.tenantId,
            email = email,
            passwordHash = hashArgon2(pwd),
            loginProvider = LoginProvider.LOCAL.value,
            googleSub = null,
            displayName = displayName?.trim()?.takeIf { it.isNotEmpty() },
            emailVerified = false,
        )
        val (orchestrator, key) = provisionOrchestrator(tenant, user.userId)

        auditOnboarding(tenant.tenantId, orchestrator.agentId, "auth:register")
        log.info("registered user {} + tenant {} + orchestrator {}", user.userId, tenant.tenantId, orchestrator.agentId)
        return RegisterResult(
            userId = user.userId,
            tenantId = tenant.tenantId,
            orchestratorAgentId = orchestrator.agentId,
            apiKeyId = key.keyId,
            apiKey = key.rawKey,
            keyPrefix = key.keyPrefix,
        )
    }

    // ── 2. Login (email + password) ────────────────────────────────────────────────────────

    fun login(emailRaw: String?, password: String?): LoginResult {
        val email = normalizeEmail(emailRaw)
        val pwd = password?.takeIf { it.isNotBlank() }
            ?: throw invalidCredentials()
        val user = userRepository.findByEmail(email) ?: throw invalidCredentials()
        if (user.status != "active") throw ApiException.forbidden("Account is not active")
        val hash = user.passwordHash
            ?: throw ApiException("USE_GOOGLE_LOGIN", HttpStatus.BAD_REQUEST, "This account uses Google sign-in")
        if (!verifyArgon2(hash, pwd)) throw invalidCredentials()

        return issueOrchestratorSession(user)
    }

    // ── 3. Google OAuth2 ─────────────────────────────────────────────────────────────────────

    /** Build the Google authorization URL and stash the single-use `state` in Valkey (5-min TTL). */
    fun googleAuthUrl(): GoogleAuthUrl {
        requireGoogleEnabled()
        val state = randomUrlToken()
        storeGoogleState(state)
        val scope = googleProps.scopes.joinToString(" ")
        val url = buildString {
            append(googleProps.authEndpoint)
            append("?response_type=code")
            append("&client_id=").append(enc(googleProps.clientId))
            append("&redirect_uri=").append(enc(googleProps.redirectUri))
            append("&scope=").append(enc(scope))
            append("&state=").append(enc(state))
            append("&access_type=online&prompt=select_account")
        }
        return GoogleAuthUrl(url = url, state = state)
    }

    /** Handle the Google callback: validate state, exchange code, find/provision the user, issue session. */
    fun handleGoogleCallback(code: String?, state: String?): LoginResult {
        requireGoogleEnabled()
        if (code.isNullOrBlank()) throw ApiException.validation("missing code", mapOf("field" to "code"))
        if (state.isNullOrBlank() || !consumeGoogleState(state)) {
            throw ApiException.unauthorized("Invalid or expired OAuth state")
        }
        val accessToken = exchangeGoogleCode(code)
        val profile = fetchGoogleUserInfo(accessToken)
        val email = normalizeEmail(profile.email)

        // Find by Google subject, else by email (link), else provision a brand-new tenant+orchestrator.
        val user = userRepository.findByGoogleSub(profile.sub)
            ?: userRepository.findByEmail(email)?.also { userRepository.linkGoogleSub(it.userId, profile.sub) }
            ?: provisionGoogleUser(email, profile.sub, profile.name)
        return issueOrchestratorSession(user)
    }

    // ── shared issuance ──────────────────────────────────────────────────────────────────────

    /** Mint an agent JWT for the user's tenant orchestrator (the Console session identity). */
    private fun issueOrchestratorSession(user: UserRecord): LoginResult {
        val orchestrator = agentRepository.findOrchestrator(user.tenantId)
            ?: throw ApiException("NO_ORCHESTRATOR", HttpStatus.CONFLICT, "Tenant has no orchestrator agent")
        val plan = runCatching { tenantRepository.findById(user.tenantId)?.plan }.getOrNull()

        val extra = buildMap<String, Any?> {
            put("agent_type", orchestrator.agentType)
            orchestrator.parentOrchestratorId?.let { put("parent_orchestrator_id", it.toString()) }
            put("user_id", user.userId.toString())
            plan?.let { put("plan", it) }
        }
        val minted = jwtMintService.mintAgentToken(
            agentId = orchestrator.agentId,
            tenantId = user.tenantId,
            scopes = orchestrator.allowedScopes,
            ttlSeconds = props.agentTokenTtlSeconds,
            extraClaims = extra,
        )
        recordActiveJti(orchestrator.agentId, minted.jti, minted.expiresAt)
        runCatching { userRepository.touchLastLogin(user.userId) }
        auditOnboarding(user.tenantId, orchestrator.agentId, "auth:login")

        val expiresIn = Duration.between(Instant.now(), minted.expiresAt).seconds.coerceAtLeast(1)
        return LoginResult(
            userId = user.userId,
            tenantId = user.tenantId,
            agentId = orchestrator.agentId,
            token = minted.token,
            expiresIn = expiresIn,
            scopes = orchestrator.allowedScopes,
        )
    }

    private fun provisionGoogleUser(email: String, googleSub: String, name: String?): UserRecord {
        val tenant = provisionTenant(name?.takeIf { it.isNotBlank() } ?: email.substringBefore('@'))
        seedQuotas(tenant)
        val user = userRepository.insert(
            tenantId = tenant.tenantId,
            email = email,
            passwordHash = null,
            loginProvider = LoginProvider.GOOGLE.value,
            googleSub = googleSub,
            displayName = name,
            emailVerified = true,
        )
        provisionOrchestrator(tenant, user.userId)
        log.info("provisioned google user {} + tenant {}", user.userId, tenant.tenantId)
        return user
    }

    // ── provisioning helpers (mirror OnboardingService's proven pattern) ──────────────────────

    private fun provisionTenant(name: String): Tenant {
        val plan = "free"
        val region = "us-east-1"
        tenantRepository.planDefaultLimits(plan)
            ?: throw ApiException.validation("Default plan '$plan' is not configured", mapOf("plan" to plan))
        val tenantId = UUID.randomUUID()
        return tenantTx.inPlatform { jdbc ->
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
    }

    private fun seedQuotas(tenant: Tenant) {
        runCatching {
            val limits = tenantRepository.planDefaultLimits(tenant.plan)
            if (limits != null) {
                tenantRepository.seedQuotasFromPlan(
                    tenant.tenantId, tenant.plan, limits, updatedBy = SYSTEM_USER_ID.toString(),
                )
            }
        }.onFailure { log.warn("quota seed failed for tenant {}: {}", tenant.tenantId, it.message) }
    }

    /** Create the tenant's single orchestrator + its initial api_key (raw key returned once). */
    private fun provisionOrchestrator(tenant: Tenant, ownerUserId: UUID): Pair<AgentRecord, ApiKeyService.IssuedKey> {
        val systemCaller = AgentService.Caller(
            agentId = SYSTEM_USER_ID,
            tenantId = tenant.tenantId,
            scopes = setOf("platform:admin"),
        )
        val orchestrator = agentService.createAgent(
            AgentService.CreateAgentCommand(
                name = ORCHESTRATOR_NAME,
                version = "1.0.0",
                allowedScopes = ORCHESTRATOR_DEFAULT_SCOPES,
                requestedTenantId = tenant.tenantId,
                agentType = AgentType.ORCHESTRATOR,
                ownerUserId = ownerUserId,
            ),
            systemCaller,
        )
        val key = apiKeyService.issue(
            tenantId = tenant.tenantId,
            agentId = orchestrator.agentId,
            scopes = ORCHESTRATOR_DEFAULT_SCOPES,
            name = "orchestrator-initial-key",
            expiresInDays = null,
        )
        return orchestrator to key
    }

    private fun auditOnboarding(tenantId: UUID, agentId: UUID, action: String) {
        runCatching {
            auditService.record(
                eventType = "auth.user_session",
                tenantId = tenantId,
                agentId = agentId,
                action = action,
                resource = "agent:$agentId",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for {} {}: {}", action, agentId, it.message) }
    }

    // ── Google HTTP helpers ────────────────────────────────────────────────────────────────

    private data class GoogleProfile(val sub: String, val email: String, val name: String?)

    private fun exchangeGoogleCode(code: String): String {
        val form = mapOf(
            "code" to code,
            "client_id" to googleProps.clientId,
            "client_secret" to googleProps.clientSecret,
            "redirect_uri" to googleProps.redirectUri,
            "grant_type" to "authorization_code",
        ).entries.joinToString("&") { "${enc(it.key)}=${enc(it.value)}" }
        val req = HttpRequest.newBuilder(URI.create(googleProps.tokenEndpoint))
            .header("Content-Type", "application/x-www-form-urlencoded")
            .timeout(Duration.ofSeconds(10))
            .POST(HttpRequest.BodyPublishers.ofString(form))
            .build()
        val resp = send(req)
        if (resp.statusCode() !in 200..299) {
            throw ApiException.unauthorized("Google token exchange failed")
        }
        val node = objectMapper.readTree(resp.body())
        return node.get("access_token")?.asText()
            ?: throw ApiException.unauthorized("Google token exchange returned no access_token")
    }

    private fun fetchGoogleUserInfo(accessToken: String): GoogleProfile {
        val req = HttpRequest.newBuilder(URI.create(googleProps.userInfoEndpoint))
            .header("Authorization", "Bearer $accessToken")
            .timeout(Duration.ofSeconds(10))
            .GET()
            .build()
        val resp = send(req)
        if (resp.statusCode() !in 200..299) throw ApiException.unauthorized("Google userinfo failed")
        val node = objectMapper.readTree(resp.body())
        val sub = node.get("sub")?.asText()?.takeIf { it.isNotBlank() }
            ?: throw ApiException.unauthorized("Google userinfo missing sub")
        val email = node.get("email")?.asText()?.takeIf { it.isNotBlank() }
            ?: throw ApiException.unauthorized("Google userinfo missing email")
        return GoogleProfile(sub = sub, email = email, name = node.get("name")?.asText())
    }

    private fun send(req: HttpRequest): HttpResponse<String> = try {
        httpClient.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8))
    } catch (ex: Exception) {
        log.warn("google http call failed: {}", ex.message)
        throw ApiException("GOOGLE_UPSTREAM_ERROR", HttpStatus.BAD_GATEWAY, "Google sign-in is temporarily unavailable")
    }

    private fun requireGoogleEnabled() {
        if (!googleProps.enabled) {
            throw ApiException(
                "GOOGLE_OAUTH_NOT_CONFIGURED", HttpStatus.NOT_IMPLEMENTED,
                "Google sign-in is not configured on this deployment",
            )
        }
    }

    // ── Valkey-backed single-use OAuth state ─────────────────────────────────────────────────

    private fun storeGoogleState(state: String) {
        val r = redis ?: return // fail-open: without Valkey, callback state check is skipped-by-absence
        runCatching {
            r.opsForValue().set("cypherx:auth:google:state:$state", "1", Duration.ofSeconds(googleProps.stateTtlSeconds))
        }.onFailure { log.debug("google state store skipped: {}", it.message) }
    }

    /** Returns true if the state was present (and deletes it). When Valkey is absent, accept (dev). */
    private fun consumeGoogleState(state: String): Boolean {
        val r = redis ?: return true
        return runCatching { r.delete("cypherx:auth:google:state:$state") == true }.getOrElse { true }
    }

    private fun recordActiveJti(agentId: UUID, jti: UUID, expiresAt: Instant) {
        val r = redis ?: return
        runCatching {
            val key = "agent-active-jtis:$agentId"
            r.opsForSet().add(key, jti.toString())
            val ttl = Duration.between(Instant.now(), expiresAt).coerceAtLeast(Duration.ofSeconds(1))
            val existing = r.getExpire(key)
            if (existing == null || existing < ttl.seconds) r.expire(key, ttl)
        }.onFailure { log.debug("recordActiveJti skipped: {}", it.message) }
    }

    // ── small helpers ──────────────────────────────────────────────────────────────────────

    private fun normalizeEmail(raw: String?): String {
        val email = raw?.trim()?.lowercase().orEmpty()
        if (email.isEmpty() || !EMAIL_PATTERN.matcher(email).matches()) {
            throw ApiException.validation("A valid email is required", mapOf("field" to "email"))
        }
        return email
    }

    private fun hashArgon2(secret: String): String {
        val chars = secret.toCharArray()
        return try {
            argon2.hash(ARGON2_ITERATIONS, ARGON2_MEMORY_KB, ARGON2_PARALLELISM, chars)
        } finally {
            argon2.wipeArray(chars)
        }
    }

    private fun verifyArgon2(hash: String, secret: String): Boolean {
        val chars = secret.toCharArray()
        return try {
            argon2.verify(hash, chars)
        } catch (ex: Exception) {
            false
        } finally {
            argon2.wipeArray(chars)
        }
    }

    private fun randomUrlToken(): String {
        val bytes = ByteArray(24)
        rng.nextBytes(bytes)
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    private fun enc(v: String): String = URLEncoder.encode(v, StandardCharsets.UTF_8)

    private fun invalidCredentials() =
        ApiException("INVALID_CREDENTIALS", HttpStatus.UNAUTHORIZED, "Invalid email or password")

    private companion object {
        val log = LoggerFactory.getLogger(UserAuthService::class.java)
        const val ORCHESTRATOR_NAME = "orchestrator"
        const val MIN_PASSWORD_LEN = 8
        const val ARGON2_ITERATIONS = 3
        const val ARGON2_MEMORY_KB = 65536
        const val ARGON2_PARALLELISM = 1
        val EMAIL_PATTERN: Pattern = Pattern.compile("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")
    }
}
