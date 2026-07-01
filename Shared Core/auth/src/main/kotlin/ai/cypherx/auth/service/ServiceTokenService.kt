package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.config.ServiceAuthProperties
import ai.cypherx.auth.domain.PLATFORM_TENANT_ID
import ai.cypherx.auth.repo.ServiceAclRepository
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.UUID

/**
 * Issues internal service tokens (Contract 12 / Component 8b) for `POST /v1/service-tokens`.
 *
 * First-cycle authentication is bootstrap-secret only:
 *  - `X-Service-Name` identifies the caller.
 *  - `X-Service-Bootstrap-Secret` is compared (constant-time) to the per-service secret from
 *    [ServiceAuthProperties.bootstrapSecrets]. Mismatch / unknown service -> 401.
 *
 * Scopes are derived SERVER-SIDE (the caller never supplies them): the union of `allowed_scopes`
 * across every `auth.service_acl` row whose `caller_service` matches the service name. A caller
 * with no ACL rows gets 403 FORBIDDEN.
 *
 * The token is minted via [JwtMintService.mintServiceToken] (`sub=svc:<name>`, `aud=["*"]`,
 * `service_name`, `scopes`, TTL clamped to <= 300s).
 */
@Service
class ServiceTokenService(
    private val serviceAuthProps: ServiceAuthProperties,
    private val serviceAclRepository: ServiceAclRepository,
    private val jwtMintService: JwtMintService,
    private val props: AuthProperties,
    private val auditService: AuditService,
) {

    /**
     * Authenticate the bootstrap secret, derive scopes from the ACL, and mint a service token.
     *
     * @param serviceName   the `X-Service-Name` header.
     * @param bootstrapSecret the `X-Service-Bootstrap-Secret` header.
     * @param tenantId      optional tenant on whose behalf the call is made (request body).
     * @param onBehalfOf    optional agent that triggered the call (request body).
     * @param requestedTtlSeconds optional TTL; clamped to (1, serviceTokenTtlSeconds] (300s).
     */
    fun issue(
        serviceName: String,
        bootstrapSecret: String?,
        tenantId: UUID? = null,
        onBehalfOf: UUID? = null,
        requestedTtlSeconds: Long? = null,
    ): IssuedServiceToken {
        authenticate(serviceName, bootstrapSecret)

        val scopes = serviceAclRepository.unionAllowedScopes(serviceName)
        if (scopes.isEmpty()) {
            throw ApiException.forbidden(
                "Service '$serviceName' has no service_acl entry and may not mint a service token",
                mapOf("service_name" to serviceName),
            )
        }

        val ttl = requestedTtlSeconds ?: props.serviceTokenTtlSeconds
        val minted = jwtMintService.mintServiceToken(
            serviceName = serviceName,
            scopes = scopes,
            tenantId = tenantId,
            onBehalfOf = onBehalfOf,
            ttlSeconds = ttl,
        )

        val expiresIn = ttl.coerceIn(1, props.serviceTokenTtlSeconds)
        log.info(
            "service_token.issued service={} kid={} jti={} scopes={}",
            serviceName, minted.kid, minted.jti, scopes,
        )

        // Durable audit (Component 6 — issuance-event coverage). A service token may be platform-level
        // (no tenant), so attribute it to the on-behalf tenant when present, else the platform sentinel.
        runCatching {
            auditService.record(
                eventType = "service_token.issued",
                tenantId = tenantId ?: PLATFORM_TENANT_ID,
                agentId = onBehalfOf,
                action = "service_token:issue",
                resource = "svc:$serviceName:jti:${minted.jti}",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for service_token.issued {}: {}", minted.jti, it.message) }

        return IssuedServiceToken(
            token = minted.token,
            expiresIn = expiresIn,
            kid = minted.kid.toString(),
            aud = listOf("*"),
            jti = minted.jti,
        )
    }

    /** Constant-time compare of the presented bootstrap secret against the configured one. */
    private fun authenticate(serviceName: String, presented: String?) {
        val expected = serviceAuthProps.bootstrapSecrets[serviceName]
        if (expected.isNullOrEmpty() || presented.isNullOrEmpty() || !constantTimeEquals(expected, presented)) {
            // Do NOT leak whether the service is known vs the secret is wrong.
            throw ApiException.unauthorized("Invalid service name or bootstrap secret")
        }
    }

    private fun constantTimeEquals(a: String, b: String): Boolean =
        MessageDigest.isEqual(
            a.toByteArray(StandardCharsets.UTF_8),
            b.toByteArray(StandardCharsets.UTF_8),
        )

    /** Result of a successful service-token issuance. */
    data class IssuedServiceToken(
        val token: String,
        val expiresIn: Long,
        val kid: String,
        val aud: List<String>,
        val jti: UUID,
    )

    private companion object {
        val log = LoggerFactory.getLogger(ServiceTokenService::class.java)
    }
}
