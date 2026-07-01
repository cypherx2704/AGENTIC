package ai.cypherx.auth.api

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.RevocationReason
import ai.cypherx.auth.service.RevocationService
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import org.springframework.http.ResponseEntity
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController
import java.time.Duration
import java.time.Instant
import java.util.UUID

/**
 * Live token-revocation endpoints (Component 3c, Phase 2):
 *
 *   POST /v1/tokens/revoke                        { jti, reason }      → 204 No Content
 *   POST /v1/agents/{agent_id}/revoke-all-tokens  { reason }          → 200 { revoked_count }
 *
 * Both require scope `agent:revoke` OR `platform:admin` (phase doc). Scope enforcement is done here
 * by inspecting the SecurityContext authorities (method-security is not enabled in Core) — the
 * [ai.cypherx.auth.web.AgentJwtAuthFilter] has already attached `SCOPE_*` authorities from the
 * verified caller JWT and set the principal to the caller's `sub` (agent_id).
 *
 * `tenant_id` and the acting principal are taken from the CALLER's verified JWT — never from the
 * body (Contract 13 anti-pattern). For a bare `jti` we cannot recover the target token's exact
 * `exp`, so the Valkey deny entry + durable row are bounded by the max agent TTL from now (the same
 * safe upper bound the phase doc uses for `kid-poisoned` and revoke-all) — long enough to cover any
 * still-presentable token bearing that jti.
 */
@RestController
@RequestMapping("/v1")
class RevocationController(
    private val revocationService: RevocationService,
    private val jwtMintService: JwtMintService,
    private val props: AuthProperties,
) {

    /** POST /v1/tokens/revoke — revoke one jti. */
    @PostMapping("/tokens/revoke")
    fun revokeToken(@RequestBody body: RevokeTokenRequest): ResponseEntity<Void> {
        val caller = requireRevokeScope()
        val jti = parseUuid(body.jti, "jti")
        val reason = parseReason(body.reason)
        val tokenExp = Instant.now().plusSeconds(props.agentTokenTtlSeconds)

        revocationService.revokeJti(
            jti = jti,
            tenantId = caller.tenantId,
            agentId = null,
            reason = reason,
            revokedBy = caller.principalId,
            tokenExp = tokenExp,
        )
        return ResponseEntity.noContent().build()
    }

    /** POST /v1/agents/{agent_id}/revoke-all-tokens — revoke every live token for the agent. */
    @PostMapping("/agents/{agentId}/revoke-all-tokens")
    fun revokeAllTokens(
        @PathVariable agentId: String,
        @RequestBody(required = false) body: RevokeAllRequest?,
    ): ResponseEntity<Map<String, Any>> {
        val caller = requireRevokeScope()
        val targetAgent = parseUuid(agentId, "agent_id")
        val reason = parseReason(body?.reason ?: RevocationReason.COMPROMISED.value)

        val count = revocationService.revokeAllForAgent(
            agentId = targetAgent,
            tenantId = caller.tenantId,
            reason = reason,
            revokedBy = caller.principalId,
            defaultTokenTtl = Duration.ofSeconds(props.agentTokenTtlSeconds),
        )
        return ResponseEntity.ok(mapOf("revoked_count" to count))
    }

    // ── Auth / parsing helpers ─────────────────────────────────────────────────────────────

    private data class Caller(val principalId: UUID, val tenantId: UUID)

    /**
     * Enforce `agent:revoke` OR `platform:admin`, and resolve the caller's principal + tenant from
     * the verified JWT (stored as the SecurityContext credentials by the auth filter). Throws
     * Contract 2 401/403 otherwise.
     */
    private fun requireRevokeScope(): Caller {
        val auth = SecurityContextHolder.getContext().authentication
            ?: throw ApiException.unauthorized("Authentication required")

        val authorities = auth.authorities.map { it.authority }.toSet()
        val permitted = authorities.contains("SCOPE_agent:revoke") || authorities.contains("SCOPE_platform:admin")
        if (!permitted) {
            throw ApiException.forbidden(
                "Caller lacks token-revocation scope",
                mapOf("required_any" to listOf("agent:revoke", "platform:admin")),
            )
        }

        // The filter stores the raw bearer token as credentials; parse it for tenant_id/sub.
        val rawToken = auth.credentials as? String
            ?: throw ApiException.unauthorized("Caller token unavailable")
        val claims = jwtMintService.verify(rawToken)?.jwtClaimsSet
            ?: throw ApiException.unauthorized("Caller token invalid")

        val tenantId = (claims.getStringClaim("tenant_id"))?.let { runCatching { UUID.fromString(it) }.getOrNull() }
            ?: throw ApiException.forbidden("Caller token has no tenant_id")
        val principalId = (claims.subject ?: claims.getStringClaim("agent_id"))
            ?.let { runCatching { UUID.fromString(it) }.getOrNull() }
            ?: throw ApiException.forbidden("Caller token has no agent subject")

        return Caller(principalId = principalId, tenantId = tenantId)
    }

    private fun parseUuid(value: String?, field: String): UUID {
        if (value.isNullOrBlank()) throw ApiException.validation("Missing required field: $field", mapOf("field" to field))
        return runCatching { UUID.fromString(value) }.getOrElse {
            throw ApiException.validation("Invalid UUID for $field", mapOf("field" to field))
        }
    }

    private fun parseReason(value: String?): RevocationReason {
        if (value.isNullOrBlank()) throw ApiException.validation("Missing required field: reason", mapOf("field" to "reason"))
        return runCatching { RevocationReason.from(value) }.getOrElse {
            throw ApiException.validation(
                "Unknown revocation reason",
                mapOf("field" to "reason", "allowed" to RevocationReason.entries.map { it.value }),
            )
        }
    }

    // ── Request bodies ─────────────────────────────────────────────────────────────────────

    data class RevokeTokenRequest(
        val jti: String?,
        val reason: String?,
    )

    data class RevokeAllRequest(
        val reason: String?,
    )
}
