package ai.cypherx.auth.api

import ai.cypherx.auth.domain.SYSTEM_USER_ID
import ai.cypherx.auth.service.ApiKeyService
import ai.cypherx.auth.web.ApiException
import ai.cypherx.auth.web.TraceContextFilter
import com.fasterxml.jackson.annotation.JsonInclude
import org.slf4j.MDC
import org.springframework.http.HttpStatus
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.web.bind.annotation.DeleteMapping
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.ResponseStatus
import org.springframework.web.bind.annotation.RestController
import java.time.Instant
import java.util.UUID

/**
 * API-key lifecycle for agents (Component 2).
 *
 *   POST   /v1/agents/{agent_id}/keys                 Issue a new key (raw secret returned ONCE)
 *   GET    /v1/agents/{agent_id}/keys                 List keys (never the secret)
 *   DELETE /v1/agents/{agent_id}/keys/{key_id}        Revoke a key
 *   POST   /v1/agents/{agent_id}/keys/{key_id}/rotate Rotate a key (new secret ONCE; 24h dual-validity)
 *
 * These routes are authenticated-by-default (SecurityConfig `anyRequest authenticated`) and gated
 * here with `agent:write` (mutations) / `agent:read` (list). The tenant is resolved from the
 * `X-Tenant-ID` header that Kong injects from the caller's JWT (surfaced via MDC by
 * [TraceContextFilter]) — NEVER from the request body (Contract 13 anti-pattern).
 *
 * Errors are thrown as [ApiException] and rendered as the Contract 2 envelope by the Core
 * GlobalExceptionHandler.
 */
@RestController
@RequestMapping("/v1/agents/{agentId}/keys")
class ApiKeyController(
    private val apiKeyService: ApiKeyService,
) {

    // ── Requests / responses ────────────────────────────────────────────────────────────────

    data class CreateKeyRequest(
        val scopes: List<String> = emptyList(),
        val name: String? = null,
        val expiresInDays: Long? = null,
    )

    /** Issuance response — the ONLY place `api_key` (the raw secret) is ever returned. */
    @JsonInclude(JsonInclude.Include.NON_NULL)
    data class CreateKeyResponse(
        val keyId: UUID,
        val apiKey: String,
        val keyPrefix: String,
        val scopes: List<String>,
        val expiresAt: Instant?,
        val createdAt: Instant,
    )

    @JsonInclude(JsonInclude.Include.NON_NULL)
    data class KeyListItem(
        val keyId: UUID,
        val agentId: UUID,
        val keyPrefix: String,
        val name: String?,
        val scopes: List<String>,
        val status: String,
        val expiresAt: Instant?,
        val lastUsedAt: Instant?,
        val createdAt: Instant,
        val revokedAt: Instant?,
    )

    data class KeyListResponse(val keys: List<KeyListItem>)

    /** Optional overrides for a rotation; all absent = inherit the old key's scopes/name. */
    data class RotateKeyRequest(
        val scopes: List<String>? = null,
        val name: String? = null,
        val expiresInDays: Long? = null,
    )

    /**
     * Rotation response: the NEW key's raw secret (shown ONCE) plus the previous key id and the
     * instant its 24h dual-validity grace ends (until then the old key still exchanges for tokens).
     */
    @JsonInclude(JsonInclude.Include.NON_NULL)
    data class RotateKeyResponse(
        val keyId: UUID,
        val apiKey: String,
        val keyPrefix: String,
        val scopes: List<String>,
        val expiresAt: Instant?,
        val createdAt: Instant,
        val previousKeyId: UUID,
        val previousKeyExpiresAt: Instant,
    )

    // ── Endpoints ─────────────────────────────────────────────────────────────────────────

    /** Issue a new API key for the agent. Returns the raw secret once. */
    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    fun create(
        @PathVariable agentId: UUID,
        @RequestBody body: CreateKeyRequest,
    ): CreateKeyResponse {
        requireScope("agent:write")
        val tenantId = requireTenant()
        val scopes = body.scopes.map { it.trim() }.filter { it.isNotEmpty() }
        if (scopes.isEmpty()) {
            throw ApiException.validation(
                "scopes must contain at least one non-empty scope",
                mapOf("field" to "scopes"),
            )
        }
        val issued = apiKeyService.issue(
            tenantId = tenantId,
            agentId = agentId,
            scopes = scopes,
            name = body.name?.takeIf { it.isNotBlank() },
            expiresInDays = body.expiresInDays,
        )
        return CreateKeyResponse(
            keyId = issued.keyId,
            apiKey = issued.rawKey,
            keyPrefix = issued.keyPrefix,
            scopes = issued.scopes,
            expiresAt = issued.expiresAt,
            createdAt = issued.createdAt,
        )
    }

    /** List the agent's keys (no secrets). */
    @GetMapping
    fun list(@PathVariable agentId: UUID): KeyListResponse {
        requireScope("agent:read")
        val tenantId = requireTenant()
        val items = apiKeyService.list(tenantId, agentId).map {
            KeyListItem(
                keyId = it.keyId,
                agentId = it.agentId,
                keyPrefix = it.keyPrefix,
                name = it.name,
                scopes = it.scopes,
                status = it.status,
                expiresAt = it.expiresAt,
                lastUsedAt = it.lastUsedAt,
                createdAt = it.createdAt,
                revokedAt = it.revokedAt,
            )
        }
        return KeyListResponse(items)
    }

    /** Revoke a key. 204 on success (idempotent if already revoked); 404 if unknown. */
    @DeleteMapping("/{keyId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    fun revoke(
        @PathVariable agentId: UUID,
        @PathVariable keyId: UUID,
    ) {
        requireScope("agent:write")
        val tenantId = requireTenant()
        apiKeyService.revoke(tenantId, agentId, keyId, revokedBy = callerId())
    }

    /**
     * Rotate a key: issue a NEW key (raw secret returned ONCE) and keep the OLD key valid for a 24h
     * dual-validity grace (its status stays `active`, `expires_at` set to now+24h). 201 Created.
     * 404 if the key is unknown / not this agent's; 409 if the key is already revoked or expired.
     */
    @PostMapping("/{keyId}/rotate")
    @ResponseStatus(HttpStatus.CREATED)
    fun rotate(
        @PathVariable agentId: UUID,
        @PathVariable keyId: UUID,
        @RequestBody(required = false) body: RotateKeyRequest?,
    ): RotateKeyResponse {
        requireScope("agent:write")
        val tenantId = requireTenant()
        val scopesOverride = body?.scopes?.map { it.trim() }?.filter { it.isNotEmpty() }
        val rotated = apiKeyService.rotate(
            tenantId = tenantId,
            agentId = agentId,
            keyId = keyId,
            scopesOverride = scopesOverride,
            nameOverride = body?.name?.takeIf { it.isNotBlank() },
            expiresInDays = body?.expiresInDays,
            rotatedBy = callerId(),
        )
        return RotateKeyResponse(
            keyId = rotated.newKey.keyId,
            apiKey = rotated.newKey.rawKey,
            keyPrefix = rotated.newKey.keyPrefix,
            scopes = rotated.newKey.scopes,
            expiresAt = rotated.newKey.expiresAt,
            createdAt = rotated.newKey.createdAt,
            previousKeyId = rotated.previousKeyId,
            previousKeyExpiresAt = rotated.previousKeyExpiresAt,
        )
    }

    // ── helpers ───────────────────────────────────────────────────────────────────────────

    /**
     * Programmatic scope guard. The Core [AgentJwtAuthFilter] sets authorities as `SCOPE_<scope>`.
     * We enforce here rather than via `@PreAuthorize` because method security is not enabled in the
     * (locked) SecurityConfig — so annotation-based checks would be silently ignored. A
     * `platform:admin` token satisfies any scope. Missing scope -> Contract 2 403.
     */
    private fun requireScope(scope: String) {
        val authorities = SecurityContextHolder.getContext().authentication
            ?.authorities
            ?.map { it.authority }
            ?.toSet()
            ?: emptySet()
        if ("SCOPE_$scope" in authorities || "SCOPE_platform:admin" in authorities) return
        throw ApiException.forbidden(
            "Caller lacks required scope: $scope",
            mapOf("required" to scope),
        )
    }

    /**
     * Resolve the caller's tenant from the `X-Tenant-ID` header (placed in MDC by
     * [TraceContextFilter] — Kong injects it from the verified JWT). 401 if absent/malformed: a
     * tenant-scoped key operation cannot proceed without a tenant context (RLS would deny anyway).
     */
    private fun requireTenant(): UUID {
        val raw = MDC.get(TraceContextFilter.MDC_TENANT_ID)
            ?: throw ApiException.unauthorized("Missing tenant context (X-Tenant-ID)")
        return runCatching { UUID.fromString(raw) }
            .getOrElse { throw ApiException.unauthorized("Malformed tenant context") }
    }

    /** Best-effort caller identity (the authenticated agent's id) for revoked_by; falls back to system. */
    private fun callerId(): UUID {
        val principal = SecurityContextHolder.getContext().authentication?.name
        return principal?.let { runCatching { UUID.fromString(it) }.getOrNull() } ?: SYSTEM_USER_ID
    }
}
