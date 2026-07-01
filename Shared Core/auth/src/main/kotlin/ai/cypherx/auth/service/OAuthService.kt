package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.ServiceClientStatus
import ai.cypherx.auth.repo.NewServiceClient
import ai.cypherx.auth.repo.NewUpstreamIssuer
import ai.cypherx.auth.repo.ServiceClientRepository
import ai.cypherx.auth.repo.ServiceClientRow
import ai.cypherx.auth.repo.UpstreamIssuerRepository
import ai.cypherx.auth.repo.UpstreamIssuerRow
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import com.nimbusds.jose.JWSAlgorithm
import com.nimbusds.jose.jwk.source.JWKSourceBuilder
import com.nimbusds.jose.proc.JWSVerificationKeySelector
import com.nimbusds.jose.proc.SecurityContext
import com.nimbusds.jwt.proc.DefaultJWTProcessor
import de.mkammerer.argon2.Argon2Factory
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.net.URI
import java.time.Instant
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * OAuth2 `client_credentials` token issuance for external service clients (Component 8b-ext,
 * Contract 12 Mode 3). Two authentication modes, both resulting in a Contract 12 service JWT minted
 * via [JwtMintService.mintServiceToken] with `sub = svc-ext:<client_id>`:
 *
 *  - Mode A (static secret): `client_id` + `client_secret`, secret verified against the Argon2id
 *    hash in `auth.service_clients`.
 *  - Mode B (federated OIDC, RFC 7521): `client_assertion` (a JWT from GitHub Actions / GCP / AWS),
 *    verified against the registered `auth.upstream_service_issuers.jwks_uri` and `required_claims`.
 *
 * In both modes the requested scopes MUST be a subset of the client's `allowed_scopes`, and the
 * requested audience MUST be one of the client's `allowed_audiences`. External tokens are TTL
 * <= 3600s ([AuthProperties.agentTokenTtlSeconds]).
 */
@Service
class OAuthService(
    private val serviceClientRepository: ServiceClientRepository,
    private val upstreamIssuerRepository: UpstreamIssuerRepository,
    private val jwtMintService: JwtMintService,
    private val props: AuthProperties,
) {

    private val argon2 = Argon2Factory.create(Argon2Factory.Argon2Types.ARGON2id)
    private val secureRandom = java.security.SecureRandom()

    /** Cache of JWT processors per upstream JWKS URI (each holds a refreshing remote JWK set). */
    private val jwtProcessors = ConcurrentHashMap<String, DefaultJWTProcessor<SecurityContext>>()

    /** Mode A: static `client_secret` exchange. */
    fun issueWithClientSecret(
        clientId: String,
        clientSecret: String?,
        requestedAudience: String?,
        requestedScopes: List<String>,
    ): IssuedOAuthToken {
        val clientUuid = parseClientId(clientId)
        val client = serviceClientRepository.findByIdForTokenIssuance(clientUuid)
            ?: throw invalidClient("Unknown client")
        assertClientUsable(client)

        val hash = client.clientSecretHash
            ?: throw invalidClient("Client is federated-only and has no static secret")
        if (clientSecret.isNullOrEmpty() || !verifyArgon2(hash, clientSecret)) {
            throw invalidClient("Invalid client credentials")
        }
        if (!client.allowedGrantTypes.contains(GRANT_CLIENT_CREDENTIALS)) {
            throw unauthorizedClient()
        }

        val audience = resolveAudience(requestedAudience, client.allowedAudiences)
        val grantedScopes = resolveScopes(requestedScopes, client.allowedScopes)

        val minted = mint(
            subjectId = client.clientId.toString(),
            serviceName = client.name,
            tenantId = client.tenantId,
            audience = audience,
            scopes = grantedScopes,
        )
        runCatching { serviceClientRepository.touchLastUsed(client.clientId) }
        log.info("oauth_token.issued mode=client_secret client_id={} aud={} scopes={}", client.clientId, audience, grantedScopes)
        return minted
    }

    /** Mode B: federated OIDC `client_assertion` (RFC 7521) exchange. */
    fun issueWithClientAssertion(
        clientAssertion: String?,
        requestedAudience: String?,
        requestedScopes: List<String>,
    ): IssuedOAuthToken {
        if (clientAssertion.isNullOrBlank()) throw invalidClient("Missing client_assertion")

        val iss = unverifiedIssuer(clientAssertion)
            ?: throw invalidClient("client_assertion has no iss claim")
        val issuer = upstreamIssuerRepository.findByIss(iss)
            ?: throw invalidClient("Untrusted assertion issuer")
        if (!issuer.status.equals("active", ignoreCase = true)) {
            throw invalidClient("Assertion issuer is not active")
        }

        val claims = verifyAssertion(clientAssertion, issuer)
        assertRequiredClaims(claims, issuer)

        val audience = resolveAudience(requestedAudience, issuer.allowedAudiences)
        val grantedScopes = resolveScopes(requestedScopes, issuer.allowedScopes)

        // Subject identifies the federated workload (its subject claim), not a registered client row.
        val subjectId = (claims["sub"] as? String) ?: iss
        val minted = mint(
            subjectId = subjectId,
            serviceName = subjectId,
            tenantId = issuer.tenantId,
            audience = audience,
            scopes = grantedScopes,
        )
        log.info("oauth_token.issued mode=client_assertion iss={} sub={} aud={} scopes={}", iss, subjectId, audience, grantedScopes)
        return minted
    }

    // ── admin CRUD (tenant-admin scoped; tenant supplied by the controller) ─────────────────

    /**
     * Register a new external service client for [tenantId]. Generates a high-entropy secret unless
     * [federatedOnly] is true (then no static secret is stored — Mode B only). Returns the created
     * row plus the RAW secret (shown ONCE; only its Argon2id hash is persisted).
     */
    fun createServiceClient(
        tenantId: UUID,
        createdBy: UUID,
        name: String,
        allowedAudiences: List<String>,
        allowedScopes: List<String>,
        federatedOnly: Boolean,
        expiresAt: Instant?,
    ): CreatedServiceClient {
        if (name.isBlank()) throw ApiException.validation("name is required")
        if (allowedAudiences.isEmpty()) throw ApiException.validation("allowed_audiences must not be empty")
        if (allowedScopes.isEmpty()) throw ApiException.validation("allowed_scopes must not be empty")

        val clientId = UUID.randomUUID()
        val rawSecret = if (federatedOnly) null else generateSecret()
        val secretHash = rawSecret?.let { hashArgon2(it) }

        val row = serviceClientRepository.insert(
            tenantId,
            NewServiceClient(
                clientId = clientId,
                name = name,
                clientSecretHash = secretHash,
                allowedGrantTypes = listOf(GRANT_CLIENT_CREDENTIALS),
                allowedAudiences = allowedAudiences,
                allowedScopes = allowedScopes,
                createdBy = createdBy,
                expiresAt = expiresAt,
            ),
        )
        log.info("service_client.created tenant={} client_id={} federated_only={}", tenantId, clientId, federatedOnly)
        return CreatedServiceClient(row = row, rawSecret = rawSecret)
    }

    /** List the caller tenant's service clients (secret hash never surfaced). */
    fun listServiceClients(tenantId: UUID): List<ServiceClientRow> =
        serviceClientRepository.listByTenant(tenantId)

    /** Revoke a client within the caller's tenant. 404 if it does not exist for this tenant. */
    fun revokeServiceClient(tenantId: UUID, clientId: UUID) {
        if (!serviceClientRepository.revoke(tenantId, clientId)) {
            throw ApiException.notFound("Service client not found", mapOf("client_id" to clientId.toString()))
        }
        log.info("service_client.revoked tenant={} client_id={}", tenantId, clientId)
    }

    /** Rotate a client's secret. Returns the new RAW secret (shown ONCE). 404 if not found. */
    fun rotateServiceClientSecret(tenantId: UUID, clientId: UUID): String {
        serviceClientRepository.findByIdInTenant(tenantId, clientId)
            ?: throw ApiException.notFound("Service client not found", mapOf("client_id" to clientId.toString()))
        val rawSecret = generateSecret()
        if (!serviceClientRepository.updateSecretHash(tenantId, clientId, hashArgon2(rawSecret))) {
            throw ApiException.notFound("Service client not found", mapOf("client_id" to clientId.toString()))
        }
        log.info("service_client.secret_rotated tenant={} client_id={}", tenantId, clientId)
        return rawSecret
    }

    /** Register (upsert) a federated OIDC issuer for the caller's tenant. */
    fun registerUpstreamIssuer(
        tenantId: UUID,
        iss: String,
        jwksUri: String,
        requiredClaims: Map<String, Any?>,
        allowedAudiences: List<String>,
        allowedScopes: List<String>,
    ): UpstreamIssuerRow {
        if (iss.isBlank()) throw ApiException.validation("iss is required")
        if (jwksUri.isBlank()) throw ApiException.validation("jwks_uri is required")
        if (allowedAudiences.isEmpty()) throw ApiException.validation("allowed_audiences must not be empty")
        if (allowedScopes.isEmpty()) throw ApiException.validation("allowed_scopes must not be empty")

        val row = upstreamIssuerRepository.upsert(
            NewUpstreamIssuer(
                iss = iss,
                tenantId = tenantId,
                jwksUri = jwksUri,
                requiredClaims = requiredClaims,
                allowedAudiences = allowedAudiences,
                allowedScopes = allowedScopes,
            ),
        )
        log.info("upstream_service_issuer.registered tenant={} iss={}", tenantId, iss)
        return row
    }

    private fun generateSecret(): String {
        val bytes = ByteArray(SECRET_BYTES)
        secureRandom.nextBytes(bytes)
        return "cxsk_" + java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    private fun hashArgon2(secret: String): String {
        val chars = secret.toCharArray()
        return try {
            argon2.hash(ARGON2_ITERATIONS, ARGON2_MEMORY_KB, ARGON2_PARALLELISM, chars)
        } finally {
            argon2.wipeArray(chars)
        }
    }

    // ── internals ─────────────────────────────────────────────────────────────────────────

    private fun mint(
        subjectId: String,
        serviceName: String,
        tenantId: UUID,
        audience: String,
        scopes: List<String>,
    ): IssuedOAuthToken {
        // Service JWT (Contract 12), sub forced to svc-ext:<id>, aud narrowed to the target service.
        val ttl = props.agentTokenTtlSeconds.coerceIn(1, MAX_EXTERNAL_TTL_SECONDS)
        val minted = jwtMintService.mintServiceToken(
            serviceName = serviceName,
            scopes = scopes,
            tenantId = tenantId,
            ttlSeconds = ttl,
            extraClaims = mapOf(
                "sub" to "svc-ext:$subjectId",
                "aud" to listOf(audience),
            ),
        )
        return IssuedOAuthToken(
            accessToken = minted.token,
            expiresIn = ttl,
            scope = scopes.joinToString(" "),
            jti = minted.jti,
            kid = minted.kid.toString(),
        )
    }

    private fun assertClientUsable(client: ServiceClientRow) {
        if (client.status != ServiceClientStatus.ACTIVE.value) throw invalidClient("Client is not active")
        client.expiresAt?.let { if (Instant.now().isAfter(it)) throw invalidClient("Client has expired") }
    }

    private fun resolveAudience(requested: String?, allowed: List<String>): String {
        if (requested.isNullOrBlank()) {
            return allowed.firstOrNull()
                ?: throw invalidScope("Client has no allowed audiences")
        }
        if (!allowed.contains(requested)) {
            throw ApiException(
                "INVALID_TARGET", org.springframework.http.HttpStatus.BAD_REQUEST,
                "Requested audience is not permitted for this client",
                mapOf("requested" to requested, "allowed" to allowed),
            )
        }
        return requested
    }

    private fun resolveScopes(requested: List<String>, allowed: List<String>): List<String> {
        if (requested.isEmpty()) return allowed
        val disallowed = requested.filterNot { allowed.contains(it) }
        if (disallowed.isNotEmpty()) {
            throw ApiException(
                "INVALID_SCOPE", org.springframework.http.HttpStatus.BAD_REQUEST,
                "Requested scopes exceed the client's allowed scopes",
                mapOf("disallowed" to disallowed, "allowed" to allowed),
            )
        }
        return requested
    }

    private fun verifyArgon2(hash: String, secret: String): Boolean {
        val chars = secret.toCharArray()
        return try {
            argon2.verify(hash, chars)
        } catch (ex: Exception) {
            log.debug("argon2 verify failed: {}", ex.message)
            false
        } finally {
            argon2.wipeArray(chars)
        }
    }

    private fun unverifiedIssuer(assertion: String): String? = try {
        com.nimbusds.jwt.SignedJWT.parse(assertion).jwtClaimsSet.issuer
    } catch (ex: Exception) {
        log.debug("client_assertion parse failed: {}", ex.message)
        null
    }

    private fun verifyAssertion(assertion: String, issuer: UpstreamIssuerRow): Map<String, Any?> {
        val processor = jwtProcessors.computeIfAbsent(issuer.jwksUri) { buildProcessor(it) }
        return try {
            val claims = processor.process(assertion, null as SecurityContext?)
            // signature + exp/nbf are enforced by the processor's default claims verifier.
            if (claims.issuer != issuer.iss) throw invalidClient("Assertion issuer mismatch")
            claims.claims
        } catch (ex: ApiException) {
            throw ex
        } catch (ex: Exception) {
            log.debug("client_assertion verification failed: {}", ex.message)
            throw invalidClient("client_assertion failed verification")
        }
    }

    private fun buildProcessor(jwksUri: String): DefaultJWTProcessor<SecurityContext> {
        val jwkSource = JWKSourceBuilder.create<SecurityContext>(URI.create(jwksUri).toURL()).build()
        val processor = DefaultJWTProcessor<SecurityContext>()
        processor.setJWSKeySelector(
            JWSVerificationKeySelector(
                setOf(JWSAlgorithm.RS256, JWSAlgorithm.ES256),
                jwkSource,
            ),
        )
        return processor
    }

    private fun assertRequiredClaims(claims: Map<String, Any?>, issuer: UpstreamIssuerRow) {
        val missing = issuer.requiredClaims.filter { (k, expected) ->
            val actual = claims[k]
            expected != null && actual?.toString() != expected.toString()
        }
        if (missing.isNotEmpty()) {
            throw ApiException(
                "INVALID_CLIENT", org.springframework.http.HttpStatus.UNAUTHORIZED,
                "client_assertion does not satisfy the issuer's required claims",
                mapOf("unsatisfied" to missing.keys.toList()),
            )
        }
    }

    private fun parseClientId(clientId: String): UUID =
        runCatching { UUID.fromString(clientId) }.getOrElse { throw invalidClient("Malformed client_id") }

    private fun invalidClient(message: String) =
        ApiException("INVALID_CLIENT", org.springframework.http.HttpStatus.UNAUTHORIZED, message)

    private fun unauthorizedClient() =
        ApiException(
            "UNAUTHORIZED_CLIENT", org.springframework.http.HttpStatus.BAD_REQUEST,
            "Client is not authorised for the client_credentials grant",
        )

    private fun invalidScope(message: String) =
        ApiException("INVALID_SCOPE", org.springframework.http.HttpStatus.BAD_REQUEST, message)

    /** Result of a successful OAuth2 token exchange (RFC 6749 shape mapped in the controller). */
    data class IssuedOAuthToken(
        val accessToken: String,
        val expiresIn: Long,
        val scope: String,
        val jti: UUID,
        val kid: String,
    )

    /** Result of creating/rotating a service client — the raw secret is returned ONCE. */
    data class CreatedServiceClient(
        val row: ServiceClientRow,
        val rawSecret: String?,
    )

    private companion object {
        val log = LoggerFactory.getLogger(OAuthService::class.java)
        const val GRANT_CLIENT_CREDENTIALS = "client_credentials"
        const val MAX_EXTERNAL_TTL_SECONDS = 3600L

        const val SECRET_BYTES = 32
        // Argon2id cost parameters (OWASP-aligned defaults for interactive verification).
        const val ARGON2_ITERATIONS = 3
        const val ARGON2_MEMORY_KB = 65536
        const val ARGON2_PARALLELISM = 1
    }
}
