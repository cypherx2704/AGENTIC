package ai.cypherx.auth.api

import ai.cypherx.auth.service.TokenMintService
import ai.cypherx.auth.web.ApiException
import ai.cypherx.auth.web.TraceContextFilter
import com.fasterxml.jackson.annotation.JsonProperty
import org.slf4j.MDC
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RestController
import java.util.UUID

/**
 * Token issuance (Component 3): `POST /v1/agents/{agent_id}/token`.
 *
 * This route is permit-all in the (locked) SecurityConfig — it body-authenticates via the raw
 * `api_key` rather than a Bearer JWT. The agent is identified by the path; the tenant is resolved
 * from the caller-supplied `X-Tenant-ID` header (Kong / SDK), surfaced via MDC by
 * [TraceContextFilter]. RLS makes the key/agent lookup impossible without a tenant, so the header
 * is mandatory.
 *
 * Response shape (Phase 2 §Component 3): `{ token, token_type: "Bearer", expires_in }`.
 */
@RestController
class TokenController(
    private val tokenMintService: TokenMintService,
) {

    data class TokenRequest(
        @JsonProperty("api_key")
        val apiKey: String? = null,
        val scopes: List<String>? = null,
    )

    data class TokenResponse(
        val token: String,
        @JsonProperty("token_type")
        val tokenType: String,
        @JsonProperty("expires_in")
        val expiresIn: Long,
        val scopes: List<String>,
    )

    @PostMapping("/v1/agents/{agentId}/token")
    fun issueToken(
        @PathVariable agentId: UUID,
        @RequestBody body: TokenRequest,
    ): TokenResponse {
        val apiKey = body.apiKey?.takeIf { it.isNotBlank() }
            ?: throw ApiException.unauthorized("Missing api_key", mapOf("field" to "api_key"))
        val tenantId = requireTenant()

        val minted = tokenMintService.exchange(
            tenantId = tenantId,
            agentId = agentId,
            rawApiKey = apiKey,
            requestedScopes = body.scopes ?: emptyList(),
        )
        return TokenResponse(
            token = minted.token,
            tokenType = minted.tokenType,
            expiresIn = minted.expiresIn,
            scopes = minted.scopes,
        )
    }

    /**
     * Resolve the tenant from the `X-Tenant-ID` header (placed in MDC by [TraceContextFilter]).
     * 401 if absent/malformed — a tenant-scoped exchange cannot proceed without a tenant context.
     */
    private fun requireTenant(): UUID {
        val raw = MDC.get(TraceContextFilter.MDC_TENANT_ID)
            ?: throw ApiException.unauthorized("Missing tenant context (X-Tenant-ID)")
        return runCatching { UUID.fromString(raw) }
            .getOrElse { throw ApiException.unauthorized("Malformed tenant context") }
    }
}
