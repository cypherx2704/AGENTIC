package ai.cypherx.auth.api

import ai.cypherx.auth.service.OAuthService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.annotation.JsonProperty
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController

/**
 * `POST /oauth/token` — OAuth2 `client_credentials` token endpoint (Component 8b-ext, RFC 6749).
 *
 * Public route (permit-all in SecurityConfig); the body authenticates the caller. Two modes:
 *  - Mode A: `client_id` + `client_secret` (static secret, Argon2id verified).
 *  - Mode B: `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`
 *            + `client_assertion` (federated OIDC JWT verified against a registered issuer's JWKS).
 *
 * Content-Type is `application/x-www-form-urlencoded` (RFC 6749 §2.3.1). The `scope` param is a
 * space-delimited list. Response is the RFC 6749 token shape.
 */
@RestController
class OAuthController(
    private val oAuthService: OAuthService,
) {

    @PostMapping(
        "/oauth/token",
        consumes = [MediaType.APPLICATION_FORM_URLENCODED_VALUE],
        produces = [MediaType.APPLICATION_JSON_VALUE],
    )
    fun token(
        @RequestParam("grant_type", required = false) grantType: String?,
        @RequestParam("client_id", required = false) clientId: String?,
        @RequestParam("client_secret", required = false) clientSecret: String?,
        @RequestParam("client_assertion_type", required = false) clientAssertionType: String?,
        @RequestParam("client_assertion", required = false) clientAssertion: String?,
        @RequestParam("audience", required = false) audience: String?,
        @RequestParam("scope", required = false) scope: String?,
    ): ResponseEntity<TokenResponse> {
        if (grantType != GRANT_CLIENT_CREDENTIALS) {
            throw ApiException(
                "UNSUPPORTED_GRANT_TYPE", HttpStatus.BAD_REQUEST,
                "Only the client_credentials grant is supported",
                mapOf("grant_type" to grantType),
            )
        }

        val requestedScopes = scope?.split(" ", "\t")?.map { it.trim() }?.filter { it.isNotEmpty() } ?: emptyList()

        val issued = when {
            !clientAssertion.isNullOrBlank() -> {
                if (clientAssertionType != JWT_BEARER_ASSERTION_TYPE) {
                    throw ApiException(
                        "INVALID_REQUEST", HttpStatus.BAD_REQUEST,
                        "Unsupported client_assertion_type",
                        mapOf("client_assertion_type" to clientAssertionType),
                    )
                }
                oAuthService.issueWithClientAssertion(clientAssertion, audience, requestedScopes)
            }

            !clientId.isNullOrBlank() ->
                oAuthService.issueWithClientSecret(clientId, clientSecret, audience, requestedScopes)

            else -> throw ApiException(
                "INVALID_REQUEST", HttpStatus.BAD_REQUEST,
                "Provide either client_id+client_secret or client_assertion",
            )
        }

        return ResponseEntity.status(HttpStatus.OK).body(
            TokenResponse(
                accessToken = issued.accessToken,
                tokenType = "Bearer",
                expiresIn = issued.expiresIn,
                scope = issued.scope,
            ),
        )
    }

    /** RFC 6749 §5.1 success response. */
    data class TokenResponse(
        @JsonProperty("access_token") val accessToken: String,
        @JsonProperty("token_type") val tokenType: String,
        @JsonProperty("expires_in") val expiresIn: Long,
        @JsonProperty("scope") val scope: String,
    )

    private companion object {
        const val GRANT_CLIENT_CREDENTIALS = "client_credentials"
        const val JWT_BEARER_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    }
}
