package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.google.*` configuration tree (Google "Sign in with
 * Gmail" OAuth2 / OIDC). Bound by @ConfigurationPropertiesScan.
 *
 * Keyless-by-default: locally [clientId]/[clientSecret] are EMPTY, so the Google login endpoints
 * report `GOOGLE_OAUTH_NOT_CONFIGURED` (501) rather than failing the boot. Supply real values
 * (Doppler in cloud) to enable the flow. Nothing here is a hardcoded secret.
 */
@ConfigurationProperties(prefix = "cypherx.auth.google")
data class GoogleOAuthProperties(
    /** Google OAuth2 client id (empty => Google login disabled). */
    val clientId: String = "",
    /** Google OAuth2 client secret (empty => Google login disabled). */
    val clientSecret: String = "",
    /** The redirect/callback URI registered with Google (must match exactly). */
    val redirectUri: String = "http://localhost:8080/v1/auth/oauth2/google/callback",
    /** Google's authorization endpoint. */
    val authEndpoint: String = "https://accounts.google.com/o/oauth2/v2/auth",
    /** Google's token endpoint (authorization-code exchange). */
    val tokenEndpoint: String = "https://oauth2.googleapis.com/token",
    /** Google's userinfo endpoint (email + sub). */
    val userInfoEndpoint: String = "https://openidconnect.googleapis.com/v1/userinfo",
    /** OAuth scopes requested (space-joined in the auth URL). */
    val scopes: List<String> = listOf("openid", "email", "profile"),
    /** Where the SPA should land after a successful callback (token delivered via the BFF session). */
    val postLoginRedirect: String = "http://localhost:3000/",
    /** PKCE/state TTL in seconds (state stored in Valkey, single-use). */
    val stateTtlSeconds: Long = 300,
) {
    /** True when both client id + secret are present, i.e. the flow can run. */
    val enabled: Boolean get() = clientId.isNotBlank() && clientSecret.isNotBlank()
}
