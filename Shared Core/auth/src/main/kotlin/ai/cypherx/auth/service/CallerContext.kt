package ai.cypherx.auth.service

import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.stereotype.Component
import java.util.UUID

/**
 * Resolves the authenticated caller's identity for tenant-admin endpoints.
 *
 * [ai.cypherx.auth.web.AgentJwtAuthFilter] authenticates the request, storing the raw agent JWT as
 * the [org.springframework.security.core.Authentication.getCredentials]. The filter does not surface
 * the `tenant_id` claim, so this helper re-parses the verified token (cheap, local) to extract the
 * `tenant_id` and `sub` (= acting agent id) needed to scope tenant operations.
 *
 * Scope enforcement still happens at the endpoint via `hasAuthority("SCOPE_...")`; this only
 * resolves WHICH tenant the authenticated admin belongs to.
 */
@Component
class CallerContext(
    private val jwtMintService: JwtMintService,
) {

    /** The caller's identity derived from the current request's verified agent JWT. */
    fun current(): Caller {
        val auth = SecurityContextHolder.getContext().authentication
            ?: throw ApiException.unauthorized("No authenticated caller")
        val token = auth.credentials as? String
            ?: throw ApiException.unauthorized("No bearer token on the authenticated caller")

        val jwt = jwtMintService.verify(token)
            ?: throw ApiException.unauthorized("Caller token failed verification")
        val claims = jwt.jwtClaimsSet

        val tenantId = claims.getStringClaim("tenant_id")
            ?.let { runCatching { UUID.fromString(it) }.getOrNull() }
            ?: throw ApiException.forbidden("Caller token has no tenant_id claim")

        val subject = claims.subject ?: claims.getStringClaim("agent_id")
        val agentId = subject?.let { runCatching { UUID.fromString(it) }.getOrNull() }

        return Caller(tenantId = tenantId, agentId = agentId, subject = subject)
    }

    /**
     * Resolve the caller and assert it holds at least one of [requiredScopes] (granted as
     * `SCOPE_<scope>` authorities by [ai.cypherx.auth.web.AgentJwtAuthFilter]). 403 otherwise.
     *
     * Method-level `@PreAuthorize` is not enabled service-wide, so endpoints call this to enforce
     * fine-grained scopes beyond the route-level `authenticated` rule.
     */
    fun requireAnyScope(vararg requiredScopes: String): Caller {
        val auth = SecurityContextHolder.getContext().authentication
            ?: throw ApiException.unauthorized("No authenticated caller")
        val held = auth.authorities.mapNotNull { it.authority }.toSet()
        val ok = requiredScopes.any { held.contains("SCOPE_$it") }
        if (!ok) {
            throw ApiException.forbidden(
                "Caller lacks the required scope",
                mapOf("required_any" to requiredScopes.toList()),
            )
        }
        return current()
    }

    /** Identity of the authenticated admin caller. */
    data class Caller(
        val tenantId: UUID,
        val agentId: UUID?,
        val subject: String?,
    )
}
