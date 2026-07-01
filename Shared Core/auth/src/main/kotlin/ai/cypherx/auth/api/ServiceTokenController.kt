package ai.cypherx.auth.api

import ai.cypherx.auth.service.ServiceTokenService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.annotation.JsonProperty
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestHeader
import org.springframework.web.bind.annotation.RestController
import java.util.UUID

/**
 * `POST /v1/service-tokens` — internal service-token issuance (Contract 12 / Component 8b).
 *
 * Public route (permit-all in SecurityConfig); the endpoint body-authenticates via the
 * `X-Service-Bootstrap-Secret` + `X-Service-Name` headers (first-cycle bootstrap-secret mode).
 *
 * Request body (all optional): `{ "tenant_id", "on_behalf_of", "ttl_seconds" }`.
 * Scopes are derived server-side from `auth.service_acl` (the caller does NOT specify them).
 * Response: `{ "token", "expires_in": 300, "kid", "aud": ["*"] }`.
 */
@RestController
class ServiceTokenController(
    private val serviceTokenService: ServiceTokenService,
) {

    @PostMapping("/v1/service-tokens")
    fun issue(
        @RequestHeader("X-Service-Name", required = false) serviceName: String?,
        @RequestHeader("X-Service-Bootstrap-Secret", required = false) bootstrapSecret: String?,
        @RequestBody(required = false) body: ServiceTokenRequest?,
    ): ResponseEntity<ServiceTokenResponse> {
        val name = serviceName?.takeIf { it.isNotBlank() }
            ?: throw ApiException.unauthorized("Missing X-Service-Name header")

        val req = body ?: ServiceTokenRequest()
        val issued = serviceTokenService.issue(
            serviceName = name,
            bootstrapSecret = bootstrapSecret,
            tenantId = req.tenantId,
            onBehalfOf = req.onBehalfOf,
            requestedTtlSeconds = req.ttlSeconds,
        )

        return ResponseEntity.status(HttpStatus.OK).body(
            ServiceTokenResponse(
                token = issued.token,
                expiresIn = issued.expiresIn,
                kid = issued.kid,
                aud = issued.aud,
            ),
        )
    }

    /** Request body for `POST /v1/service-tokens` (audience omitted — first cycle mints aud=["*"]). */
    data class ServiceTokenRequest(
        @JsonProperty("tenant_id") val tenantId: UUID? = null,
        @JsonProperty("on_behalf_of") val onBehalfOf: UUID? = null,
        @JsonProperty("ttl_seconds") val ttlSeconds: Long? = null,
    )

    /** Response body for `POST /v1/service-tokens`. */
    data class ServiceTokenResponse(
        @JsonProperty("token") val token: String,
        @JsonProperty("expires_in") val expiresIn: Long,
        @JsonProperty("kid") val kid: String,
        @JsonProperty("aud") val aud: List<String>,
    )
}
