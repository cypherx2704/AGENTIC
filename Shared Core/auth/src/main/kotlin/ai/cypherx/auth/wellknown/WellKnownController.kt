package ai.cypherx.auth.wellknown

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.signing.JwksService
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController

/**
 * RFC 8615 well-known endpoints (served at the origin root — NO `/v1` prefix), permit-all.
 *
 *  - `GET /.well-known/jwks.json`             — public JWKS (Contract 1 §3), built by [JwksService].
 *  - `GET /.well-known/openid-configuration`  — OIDC discovery (RFC 8414), Component 8b-disc.
 *  - `GET /.well-known/jwks-signed.json`      — signed JWKS bundle. First-cycle 503 stub (the
 *    offline RSA-4096 root signer is not provisioned in first cycle — Component 3).
 */
@RestController
class WellKnownController(
    private val jwksService: JwksService,
    private val props: AuthProperties,
) {

    /** Public JWKS: `{ "keys": [ { kty, kid, use, alg, n, e }, ... ] }`. */
    @GetMapping("/.well-known/jwks.json")
    fun jwks(): Map<String, Any> = jwksService.jwksJson()

    /** OIDC discovery document (RFC 8414). All URLs rooted at the deployment's issuerUrl. */
    @GetMapping("/.well-known/openid-configuration")
    fun openidConfiguration(): Map<String, Any> {
        val issuer = props.issuerUrl
        return linkedMapOf(
            "issuer" to issuer,
            "jwks_uri" to "$issuer/.well-known/jwks.json",
            "jwks_signed_uri" to "$issuer/.well-known/jwks-signed.json",
            "token_endpoint" to "$issuer/oauth/token",
            "introspection_endpoint" to "$issuer/oauth/introspect",
            "revocation_endpoint" to "$issuer/oauth/revoke",
            "registration_endpoint" to "$issuer/v1/admin/service-clients",
            "scopes_supported" to listOf(
                "openid", "internal:read", "internal:write",
                "llm:invoke", "rag:query", "memory:read", "memory:write",
                "tool:invoke", "guardrails:check",
                "tenant:read", "tenant:admin", "platform:admin",
            ),
            "response_types_supported" to listOf("token"),
            "grant_types_supported" to listOf("client_credentials"),
            "token_endpoint_auth_methods_supported" to listOf(
                "client_secret_post", "private_key_jwt", "client_secret_jwt",
            ),
            "subject_types_supported" to listOf("public"),
            "id_token_signing_alg_values_supported" to listOf("RS256"),
            "claims_supported" to listOf(
                "sub", "iss", "aud", "exp", "iat", "jti",
                "tenant_id", "agent_id", "api_key_id",
                "scopes", "plan", "region", "deployment_id",
            ),
            "code_challenge_methods_supported" to emptyList<String>(),
        )
    }

    /**
     * Signed JWKS bundle — first-cycle 503 stub. The offline RSA-4096 root signer + scheduled
     * re-signing job land post-first-cycle; until then external SDKs fall back to TLS-PKI JWKS.
     */
    @GetMapping("/.well-known/jwks-signed.json")
    fun jwksSigned(): ResponseEntity<Map<String, Any>> =
        ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(
            mapOf(
                "error" to mapOf(
                    "code" to "SERVICE_UNAVAILABLE",
                    "message" to "Signed JWKS bundle is not available in first cycle; use /.well-known/jwks.json",
                ),
            ),
        )
}
