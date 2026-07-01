package ai.cypherx.auth.api

import ai.cypherx.auth.service.BootstrapService
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestHeader
import org.springframework.web.bind.annotation.RestController
import java.time.Instant
import java.util.UUID

/**
 * `POST /v1/admin/bootstrap` — ONE-TIME super-admin initialisation (Phase 2 Component 1).
 *
 * Permit-all in [ai.cypherx.auth.config.SecurityConfig] (no JWT exists yet at bootstrap time);
 * authentication is the `X-Bootstrap-Token` header, validated in [BootstrapService]. After the
 * one-time sentinel is written the endpoint returns 410 Gone forever.
 *
 * Errors are thrown as `ApiException` and rendered by the Core GlobalExceptionHandler as the
 * Contract 2 envelope — this controller never hand-builds error bodies.
 */
@RestController
class BootstrapController(
    private val bootstrapService: BootstrapService,
) {

    /** Optional request body: `{ "name": "<super-admin agent name>" }`. */
    data class BootstrapRequest(
        val name: String? = null,
    )

    /**
     * Success response (201): the created super-admin agent AND its initial API key.
     * `apiKey` is the raw secret and is shown ONCE — it is never stored or retrievable again.
     * Use it to mint the first `platform:admin` JWT via POST /v1/agents/{agentId}/token.
     */
    data class BootstrapResponse(
        val agentId: UUID,
        val tenantId: UUID,
        val name: String,
        val scopes: List<String>,
        val createdAt: Instant,
        val apiKeyId: UUID,
        val apiKey: String,
        val keyPrefix: String,
    )

    @PostMapping("/v1/admin/bootstrap")
    fun bootstrap(
        @RequestHeader(name = "X-Bootstrap-Token", required = false) bootstrapToken: String?,
        @RequestBody(required = false) body: BootstrapRequest?,
    ): ResponseEntity<BootstrapResponse> {
        val result = bootstrapService.bootstrap(presentedToken = bootstrapToken, name = body?.name)
        val response = BootstrapResponse(
            agentId = result.agentId,
            tenantId = result.tenantId,
            name = result.name,
            scopes = result.allowedScopes,
            createdAt = result.createdAt,
            apiKeyId = result.apiKeyId,
            apiKey = result.apiKey,
            keyPrefix = result.keyPrefix,
        )
        return ResponseEntity.status(HttpStatus.CREATED).body(response)
    }
}
