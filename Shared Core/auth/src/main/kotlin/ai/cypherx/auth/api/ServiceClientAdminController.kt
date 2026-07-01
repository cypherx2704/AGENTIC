package ai.cypherx.auth.api

import ai.cypherx.auth.repo.ServiceClientRow
import ai.cypherx.auth.repo.UpstreamIssuerRow
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.OAuthService
import com.fasterxml.jackson.annotation.JsonInclude
import com.fasterxml.jackson.annotation.JsonProperty
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.DeleteMapping
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RestController
import java.time.Instant
import java.util.UUID

/**
 * Tenant-admin CRUD for OAuth2 external service clients and federated issuers (Component 8b-ext).
 *
 * All routes live under `/v1/admin/` and are `authenticated` by route config; the `tenant:admin`
 * (or `platform:admin`) scope is enforced in-handler via [CallerContext.requireAnyScope] since
 * method-level security is not enabled service-wide. The tenant operated on is ALWAYS the caller's
 * own tenant (from the verified JWT) — never taken from the body (Contract 13 anti-pattern).
 *
 *   POST   /v1/admin/service-clients                 register a client (returns raw secret ONCE)
 *   GET    /v1/admin/service-clients                 list the tenant's clients
 *   DELETE /v1/admin/service-clients/{id}            revoke a client
 *   POST   /v1/admin/service-clients/{id}/rotate-secret  rotate a client's secret
 *   POST   /v1/admin/upstream-service-issuers        register a federated OIDC issuer
 */
@RestController
class ServiceClientAdminController(
    private val oAuthService: OAuthService,
    private val callerContext: CallerContext,
) {

    @PostMapping("/v1/admin/service-clients")
    fun create(@RequestBody body: CreateServiceClientRequest): ResponseEntity<ServiceClientView> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        val created = oAuthService.createServiceClient(
            tenantId = caller.tenantId,
            createdBy = caller.agentId ?: caller.tenantId,
            name = body.name.orEmpty(),
            allowedAudiences = body.allowedAudiences ?: emptyList(),
            allowedScopes = body.allowedScopes ?: emptyList(),
            federatedOnly = body.federatedOnly ?: false,
            expiresAt = body.expiresAt,
        )
        return ResponseEntity.status(HttpStatus.CREATED).body(
            ServiceClientView.of(created.row, rawSecret = created.rawSecret),
        )
    }

    @GetMapping("/v1/admin/service-clients")
    fun list(): List<ServiceClientView> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        return oAuthService.listServiceClients(caller.tenantId).map { ServiceClientView.of(it) }
    }

    @DeleteMapping("/v1/admin/service-clients/{id}")
    fun revoke(@PathVariable("id") id: UUID): ResponseEntity<Void> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        oAuthService.revokeServiceClient(caller.tenantId, id)
        return ResponseEntity.noContent().build()
    }

    @PostMapping("/v1/admin/service-clients/{id}/rotate-secret")
    fun rotateSecret(@PathVariable("id") id: UUID): RotateSecretResponse {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        val secret = oAuthService.rotateServiceClientSecret(caller.tenantId, id)
        return RotateSecretResponse(clientId = id.toString(), clientSecret = secret)
    }

    @PostMapping("/v1/admin/upstream-service-issuers")
    fun registerIssuer(@RequestBody body: RegisterIssuerRequest): ResponseEntity<UpstreamIssuerView> {
        val caller = callerContext.requireAnyScope(SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN)
        val row = oAuthService.registerUpstreamIssuer(
            tenantId = caller.tenantId,
            iss = body.iss.orEmpty(),
            jwksUri = body.jwksUri.orEmpty(),
            requiredClaims = body.requiredClaims ?: emptyMap(),
            allowedAudiences = body.allowedAudiences ?: emptyList(),
            allowedScopes = body.allowedScopes ?: emptyList(),
        )
        return ResponseEntity.status(HttpStatus.CREATED).body(UpstreamIssuerView.of(row))
    }

    // ── request bodies ──────────────────────────────────────────────────────────────────────

    data class CreateServiceClientRequest(
        @JsonProperty("name") val name: String?,
        @JsonProperty("allowed_audiences") val allowedAudiences: List<String>?,
        @JsonProperty("allowed_scopes") val allowedScopes: List<String>?,
        @JsonProperty("federated_only") val federatedOnly: Boolean? = false,
        @JsonProperty("expires_at") val expiresAt: Instant? = null,
    )

    data class RegisterIssuerRequest(
        @JsonProperty("iss") val iss: String?,
        @JsonProperty("jwks_uri") val jwksUri: String?,
        @JsonProperty("required_claims") val requiredClaims: Map<String, Any?>?,
        @JsonProperty("allowed_audiences") val allowedAudiences: List<String>?,
        @JsonProperty("allowed_scopes") val allowedScopes: List<String>?,
    )

    // ── response views ──────────────────────────────────────────────────────────────────────

    @JsonInclude(JsonInclude.Include.NON_NULL)
    data class ServiceClientView(
        @JsonProperty("client_id") val clientId: String,
        @JsonProperty("tenant_id") val tenantId: String,
        @JsonProperty("name") val name: String,
        @JsonProperty("allowed_grant_types") val allowedGrantTypes: List<String>,
        @JsonProperty("allowed_audiences") val allowedAudiences: List<String>,
        @JsonProperty("allowed_scopes") val allowedScopes: List<String>,
        @JsonProperty("status") val status: String,
        @JsonProperty("created_at") val createdAt: Instant?,
        @JsonProperty("expires_at") val expiresAt: Instant?,
        @JsonProperty("last_used_at") val lastUsedAt: Instant?,
        /** Present ONLY on create/rotate — the raw secret is never stored or returned again. */
        @JsonProperty("client_secret") val clientSecret: String? = null,
    ) {
        companion object {
            fun of(row: ServiceClientRow, rawSecret: String? = null) = ServiceClientView(
                clientId = row.clientId.toString(),
                tenantId = row.tenantId.toString(),
                name = row.name,
                allowedGrantTypes = row.allowedGrantTypes,
                allowedAudiences = row.allowedAudiences,
                allowedScopes = row.allowedScopes,
                status = row.status,
                createdAt = row.createdAt,
                expiresAt = row.expiresAt,
                lastUsedAt = row.lastUsedAt,
                clientSecret = rawSecret,
            )
        }
    }

    data class RotateSecretResponse(
        @JsonProperty("client_id") val clientId: String,
        @JsonProperty("client_secret") val clientSecret: String,
    )

    data class UpstreamIssuerView(
        @JsonProperty("iss") val iss: String,
        @JsonProperty("tenant_id") val tenantId: String,
        @JsonProperty("jwks_uri") val jwksUri: String,
        @JsonProperty("required_claims") val requiredClaims: Map<String, Any?>,
        @JsonProperty("allowed_audiences") val allowedAudiences: List<String>,
        @JsonProperty("allowed_scopes") val allowedScopes: List<String>,
        @JsonProperty("status") val status: String,
        @JsonProperty("created_at") val createdAt: Instant?,
    ) {
        companion object {
            fun of(row: UpstreamIssuerRow) = UpstreamIssuerView(
                iss = row.iss,
                tenantId = row.tenantId.toString(),
                jwksUri = row.jwksUri,
                requiredClaims = row.requiredClaims,
                allowedAudiences = row.allowedAudiences,
                allowedScopes = row.allowedScopes,
                status = row.status,
                createdAt = row.createdAt,
            )
        }
    }

    private companion object {
        const val SCOPE_TENANT_ADMIN = "tenant:admin"
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
    }
}
