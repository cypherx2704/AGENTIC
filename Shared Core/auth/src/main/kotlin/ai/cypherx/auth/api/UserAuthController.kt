package ai.cypherx.auth.api

import ai.cypherx.auth.service.UserAuthService
import com.fasterxml.jackson.annotation.JsonProperty
import org.springframework.http.HttpStatus
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.ResponseStatus
import org.springframework.web.bind.annotation.RestController
import java.util.UUID

/**
 * End-user authentication surface (email/password + "Sign in with Gmail"). All routes here are
 * permit-all in [ai.cypherx.auth.config.SecurityConfig] — they body-authenticate (password / OAuth
 * code) rather than via a Bearer JWT.
 *
 * On success, login/register resolve the user's tenant and return a freshly-minted agent JWT for
 * that tenant's mandatory ORCHESTRATOR agent. The BFF stores that token in its encrypted session
 * (it never reaches the SPA) — so `logout` (drop session) and `me` (read session) are BFF concerns
 * and are NOT mirrored here.
 *
 * Google flow (token stays server-side):
 *   1. Browser → BFF → `GET /v1/auth/oauth2/google` (302 → Google).
 *   2. Google → BFF callback (the registered redirect_uri) with `?code&state`.
 *   3. BFF → `POST /v1/auth/oauth2/google/callback {code,state}` → orchestrator session JSON →
 *      BFF stores the token in the session and redirects the browser to the SPA.
 */
@RestController
class UserAuthController(
    private val userAuthService: UserAuthService,
) {

    // ── request / response shapes (snake_case via global Jackson naming) ──────────────────────

    data class RegisterRequest(
        val email: String? = null,
        val password: String? = null,
        @JsonProperty("tenant_name") val tenantName: String? = null,
        @JsonProperty("full_name") val fullName: String? = null,
    )

    data class RegisterResponse(
        @JsonProperty("user_id") val userId: UUID,
        @JsonProperty("tenant_id") val tenantId: UUID,
        @JsonProperty("orchestrator_agent_id") val orchestratorAgentId: UUID,
        @JsonProperty("api_key_id") val apiKeyId: UUID,
        @JsonProperty("api_key") val apiKey: String,
        @JsonProperty("key_prefix") val keyPrefix: String,
    )

    data class LoginRequest(
        val email: String? = null,
        val password: String? = null,
    )

    /**
     * Login/Google-exchange/refresh response. Both `token` (the <=1h access JWT) and `refresh_token`
     * are consumed by the BFF and stored in its encrypted server-side session — NEITHER reaches the
     * SPA. The BFF re-mints `token` via `POST /v1/auth/refresh` (sending `refresh_token`) before it
     * expires, so an active session never hard-expires mid-work.
     */
    data class SessionResponse(
        @JsonProperty("user_id") val userId: UUID,
        @JsonProperty("tenant_id") val tenantId: UUID,
        @JsonProperty("agent_id") val agentId: UUID,
        val token: String,
        @JsonProperty("token_type") val tokenType: String = "Bearer",
        @JsonProperty("expires_in") val expiresIn: Long,
        @JsonProperty("refresh_token") val refreshToken: String,
        @JsonProperty("refresh_expires_in") val refreshExpiresIn: Long,
        val scopes: List<String>,
    )

    data class RefreshRequest(
        @JsonProperty("refresh_token") val refreshToken: String? = null,
    )

    data class GoogleCallbackRequest(
        val code: String? = null,
        val state: String? = null,
    )

    data class GoogleStartResponse(
        @JsonProperty("auth_url") val authUrl: String,
        val state: String,
    )

    // ── 1. Register ───────────────────────────────────────────────────────────────────────────

    @PostMapping("/v1/auth/register")
    @ResponseStatus(HttpStatus.CREATED)
    fun register(@RequestBody body: RegisterRequest): RegisterResponse {
        val r = userAuthService.register(body.email, body.password, body.tenantName, body.fullName)
        return RegisterResponse(
            userId = r.userId,
            tenantId = r.tenantId,
            orchestratorAgentId = r.orchestratorAgentId,
            apiKeyId = r.apiKeyId,
            apiKey = r.apiKey,
            keyPrefix = r.keyPrefix,
        )
    }

    // ── 2. Login (email + password) ────────────────────────────────────────────────────────────

    @PostMapping("/v1/auth/login")
    fun login(@RequestBody body: LoginRequest): SessionResponse =
        userAuthService.login(body.email, body.password).toResponse()

    // ── 2b. Session refresh / logout (BFF-driven; body-authenticate via the refresh token) ──────

    /**
     * Silently renew the session: exchange a valid refresh token for a fresh <=1h access JWT (and the
     * same refresh token, with its idle window slid forward). Called by the BFF before the access
     * token expires. 401 with a Contract-2 envelope if the refresh token is missing/invalid/expired.
     */
    @PostMapping("/v1/auth/refresh")
    fun refresh(@RequestBody body: RefreshRequest): SessionResponse =
        userAuthService.refresh(body.refreshToken).toResponse()

    /** Revoke a session (logout). Idempotent — a missing/unknown token still returns 204. */
    @PostMapping("/v1/auth/logout")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    fun logout(@RequestBody(required = false) body: RefreshRequest?) {
        userAuthService.logout(body?.refreshToken)
    }

    // ── 3. Google OAuth2 ────────────────────────────────────────────────────────────────────────

    /**
     * Return the Google consent URL as JSON (state stashed in Valkey). The BFF fetches this
     * server-side and 302-redirects the browser to `auth_url` (the BFF cannot follow an auth-service
     * 302 to read the Location header through its injected fetch, so JSON is the clean contract).
     */
    @GetMapping("/v1/auth/oauth2/google")
    fun googleStart(): GoogleStartResponse {
        val url = userAuthService.googleAuthUrl()
        return GoogleStartResponse(authUrl = url.url, state = url.state)
    }

    /** Exchange the authorization code (called by the BFF) → orchestrator session JSON. */
    @PostMapping("/v1/auth/oauth2/google/callback")
    fun googleCallback(@RequestBody body: GoogleCallbackRequest): SessionResponse =
        userAuthService.handleGoogleCallback(body.code, body.state).toResponse()

    /**
     * Convenience GET for direct (non-BFF) testing: Google may redirect a browser straight here.
     * Returns the same JSON. In the production SPA the browser lands on the BFF callback instead.
     */
    @GetMapping("/v1/auth/oauth2/google/callback")
    fun googleCallbackGet(
        @RequestParam(required = false) code: String?,
        @RequestParam(required = false) state: String?,
    ): SessionResponse = userAuthService.handleGoogleCallback(code, state).toResponse()

    private fun UserAuthService.LoginResult.toResponse() = SessionResponse(
        userId = userId,
        tenantId = tenantId,
        agentId = agentId,
        token = token,
        expiresIn = expiresIn,
        refreshToken = refreshToken,
        refreshExpiresIn = refreshExpiresIn,
        scopes = scopes,
    )
}
