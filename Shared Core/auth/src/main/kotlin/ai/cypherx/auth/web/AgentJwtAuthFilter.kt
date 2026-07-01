package ai.cypherx.auth.web

import ai.cypherx.auth.service.RevocationChecker
import ai.cypherx.auth.signing.JwtMintService
import com.fasterxml.jackson.databind.ObjectMapper
import jakarta.servlet.FilterChain
import jakarta.servlet.http.HttpServletRequest
import jakarta.servlet.http.HttpServletResponse
import org.slf4j.LoggerFactory
import org.slf4j.MDC
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken
import org.springframework.security.core.authority.SimpleGrantedAuthority
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.web.filter.OncePerRequestFilter
import java.time.Instant
import java.util.UUID

/**
 * Establishes Spring Security authentication from an `Authorization: Bearer <agent-jwt>` header
 * by verifying the JWT LOCALLY (defence-in-depth — Kong already verified at the edge) via
 * [JwtMintService.verify]: signature against the JWKS public keys, exp/nbf (±clock skew),
 * `iss == issuerUrl`, and `aud` contains the platform audience.
 *
 * On success it ALSO runs the shared live-revocation MIRROR ([RevocationChecker], WP03): the same
 * `cypherx:rev:` Valkey kill-switch keys the llms/guardrails/xagent verifiers read. A revoked token
 * (jti revoked, kid poisoned, or agent revoke-all epoch newer than the token's iat) is rejected here
 * with **401 TOKEN_REVOKED** (Contract-2 envelope, chain NOT continued) — so "revoke → 401" holds at
 * Auth's own endpoints too, not only the downstream services. The check FAILS OPEN (a Valkey outage
 * accepts the token) inside [RevocationChecker]; revocation is a defence-in-depth kill-switch.
 *
 * On success (and not revoked) it sets a [UsernamePasswordAuthenticationToken] whose principal is the
 * `sub` (agent_id) and whose authorities are the `scopes` claim each prefixed `SCOPE_` (so endpoints
 * can use `hasAuthority("SCOPE_platform:admin")`).
 *
 * Verification/parse failures are SWALLOWED (no exception, no 401 here): the filter simply leaves the
 * context unauthenticated and lets the endpoint's authorization rules reject the request, keeping
 * permit-all routes (e.g. /livez, /oauth/token) working with a bad/absent token. Only a *verified but
 * revoked* token short-circuits with 401 (the caller presented a known-dead credential).
 *
 * Registered as a bean and inserted before UsernamePasswordAuthenticationFilter by
 * [ai.cypherx.auth.config.SecurityConfig].
 */
class AgentJwtAuthFilter(
    private val jwtMintService: JwtMintService,
    private val revocationChecker: RevocationChecker,
) : OncePerRequestFilter() {

    private val mapper = ObjectMapper()

    override fun doFilterInternal(
        request: HttpServletRequest,
        response: HttpServletResponse,
        filterChain: FilterChain,
    ) {
        try {
            val header = request.getHeader("Authorization")
            if (header != null && header.startsWith(BEARER_PREFIX, ignoreCase = true) &&
                SecurityContextHolder.getContext().authentication == null
            ) {
                val token = header.substring(BEARER_PREFIX.length).trim()
                val jwt = jwtMintService.verify(token)
                if (jwt != null) {
                    val claims = jwt.jwtClaimsSet
                    val agentId = claims.subject ?: claims.getStringClaim("agent_id")
                    // Shared revocation mirror — reject a verified-but-revoked token with 401.
                    val decision = revocationChecker.check(
                        jti = claims.jwtid,
                        kid = jwt.header.keyID,
                        agentId = agentId,
                        issuedAt = claims.issueTime?.toInstant(),
                    )
                    if (decision == RevocationChecker.Decision.REVOKED) {
                        writeRevoked(response)
                        return // chain NOT continued — credential is dead
                    }
                    val scopes = readScopes(claims.getClaim("scopes"))
                    val authorities = scopes.map { SimpleGrantedAuthority("SCOPE_$it") }
                    val principal = agentId ?: "agent"
                    val auth = UsernamePasswordAuthenticationToken(principal, token, authorities)
                    SecurityContextHolder.getContext().authentication = auth
                }
            }
        } catch (ex: Exception) {
            // Swallow — never block the chain on auth-parsing problems; endpoints enforce.
            log.debug("agent JWT auth filter ignored a token: {}", ex.message)
            SecurityContextHolder.clearContext()
        }
        filterChain.doFilter(request, response)
    }

    @Suppress("UNCHECKED_CAST")
    private fun readScopes(raw: Any?): List<String> = when (raw) {
        is List<*> -> raw.filterIsInstance<String>()
        is String -> raw.split(" ", ",").map { it.trim() }.filter { it.isNotEmpty() }
        else -> emptyList()
    }

    /** Render a 401 TOKEN_REVOKED in the Contract-2 envelope (filters bypass GlobalExceptionHandler). */
    private fun writeRevoked(response: HttpServletResponse) {
        response.status = HttpStatus.UNAUTHORIZED.value()
        response.contentType = MediaType.APPLICATION_JSON_VALUE
        val error = linkedMapOf<String, Any?>(
            "code" to "TOKEN_REVOKED",
            "message" to "Token has been revoked.",
            "request_id" to (MDC.get(TraceContextFilter.MDC_REQUEST_ID) ?: UUID.randomUUID().toString()),
            "trace_id" to (MDC.get(TraceContextFilter.MDC_TRACE_ID) ?: UUID.randomUUID().toString().replace("-", "")),
            "timestamp" to Instant.now().toString(),
        )
        mapper.writeValue(response.outputStream, mapOf("error" to error))
        response.outputStream.flush()
    }

    private companion object {
        val log = LoggerFactory.getLogger(AgentJwtAuthFilter::class.java)
        const val BEARER_PREFIX = "Bearer "
    }
}
