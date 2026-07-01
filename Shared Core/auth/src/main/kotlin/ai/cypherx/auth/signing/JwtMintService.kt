package ai.cypherx.auth.signing

import ai.cypherx.auth.config.AuthProperties
import com.nimbusds.jose.JOSEObjectType
import com.nimbusds.jose.JWSAlgorithm
import com.nimbusds.jose.JWSHeader
import com.nimbusds.jose.crypto.RSASSASigner
import com.nimbusds.jose.crypto.RSASSAVerifier
import com.nimbusds.jwt.JWTClaimsSet
import com.nimbusds.jwt.SignedJWT
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.time.Instant
import java.util.Date
import java.util.UUID

/**
 * The ONLY place agent / service JWTs are minted (Contract 1 / Contract 12) and the canonical
 * local verifier. RS256 only (HS256 forbidden). Every token carries `kid` in its header pointing
 * at the active signing key; tokens are verified against [SigningKeyService.verifiers] (signing +
 * verifying public keys) so in-flight tokens survive a rotation.
 *
 * TTL caps are enforced here: agent token <= [AuthProperties.agentTokenTtlSeconds] (<=3600s),
 * service token = [AuthProperties.serviceTokenTtlSeconds] (300s).
 *
 * Forward-compat: [verify] does NOT reject unknown claims (Contract 1 R-forward-compat); it
 * checks signature, exp/nbf (with clock skew), iss, and aud only.
 */
@Service
class JwtMintService(
    private val signingKeyService: SigningKeyService,
    private val props: AuthProperties,
) {

    /**
     * Mint an agent JWT (Contract 1). [scopes] are the granted scopes. [extraClaims] lets callers
     * add optional claims (e.g. agent_version, api_key_id, plan, region) without changing this API.
     * [ttlSeconds] is clamped to (0, agentTokenTtlSeconds].
     */
    fun mintAgentToken(
        agentId: UUID,
        tenantId: UUID,
        scopes: List<String>,
        ttlSeconds: Long = props.agentTokenTtlSeconds,
        extraClaims: Map<String, Any?> = emptyMap(),
    ): MintedToken {
        val ttl = ttlSeconds.coerceIn(1, props.agentTokenTtlSeconds)
        val now = Instant.now()
        val jti = UUID.randomUUID()
        val builder = JWTClaimsSet.Builder()
            .issuer(props.issuerUrl)
            .subject(agentId.toString())
            .audience(listOf(props.platformAudience))
            .issueTime(Date.from(now))
            .expirationTime(Date.from(now.plusSeconds(ttl)))
            .jwtID(jti.toString())
            .claim("tenant_id", tenantId.toString())
            .claim("agent_id", agentId.toString())
            .claim("scopes", scopes)
            .claim("deployment_id", props.deploymentId)
        extraClaims.forEach { (k, v) -> if (v != null) builder.claim(k, v) }
        return sign(builder.build(), jti, now.plusSeconds(ttl))
    }

    /**
     * Mint an internal service token (Contract 12). aud=["*"] for first cycle. ttl clamped to
     * (0, serviceTokenTtlSeconds] (300s). `sub` MUST be `svc:<name>` (caller's responsibility).
     */
    fun mintServiceToken(
        serviceName: String,
        scopes: List<String>,
        tenantId: UUID? = null,
        onBehalfOf: UUID? = null,
        ttlSeconds: Long = props.serviceTokenTtlSeconds,
        extraClaims: Map<String, Any?> = emptyMap(),
    ): MintedToken {
        val ttl = ttlSeconds.coerceIn(1, props.serviceTokenTtlSeconds)
        val now = Instant.now()
        val jti = UUID.randomUUID()
        val builder = JWTClaimsSet.Builder()
            .issuer(props.issuerUrl)
            .subject("svc:$serviceName")
            .audience(listOf("*"))
            .issueTime(Date.from(now))
            .expirationTime(Date.from(now.plusSeconds(ttl)))
            .jwtID(jti.toString())
            .claim("service_name", serviceName)
            .claim("scopes", scopes)
            .claim("deployment_id", props.deploymentId)
        tenantId?.let { builder.claim("tenant_id", it.toString()) }
        onBehalfOf?.let { builder.claim("on_behalf_of", it.toString()) }
        extraClaims.forEach { (k, v) -> if (v != null) builder.claim(k, v) }
        return sign(builder.build(), jti, now.plusSeconds(ttl))
    }

    /** Sign [claims] with the active signing key, stamping `kid` + `typ=JWT` in the header. */
    private fun sign(claims: JWTClaimsSet, jti: UUID, expiresAt: Instant): MintedToken {
        val signer = signingKeyService.activeSigner()
        val header = JWSHeader.Builder(JWSAlgorithm.RS256)
            .type(JOSEObjectType.JWT)
            .keyID(signer.kid.toString())
            .build()
        val jwt = SignedJWT(header, claims)
        jwt.sign(RSASSASigner(signer.key))
        return MintedToken(
            token = jwt.serialize(),
            jti = jti,
            kid = signer.kid,
            expiresAt = expiresAt,
        )
    }

    /**
     * Verify a token LOCALLY: resolve its header `kid` against the verifiable key set, check the
     * RS256 signature, then validate exp/nbf (±clockSkew), iss == issuerUrl, and aud. Returns the
     * parsed [SignedJWT] on success, or null on ANY failure (callers decide how to react — the
     * Agent JWT filter swallows failures and lets the endpoint enforce).
     *
     * @param requireAudience the audience that must be present in `aud`. Defaults to the platform
     *        audience; pass "*" / a service name to accept service tokens.
     */
    fun verify(token: String, requireAudience: String = props.platformAudience): SignedJWT? {
        return try {
            val jwt = SignedJWT.parse(token)
            val kid = jwt.header.keyID ?: return null
            val verifierKey = signingKeyService.verifierFor(kid) ?: return null
            if (!jwt.verify(RSASSAVerifier(verifierKey.toRSAPublicKey()))) return null

            val claims = jwt.jwtClaimsSet
            val now = Instant.now()
            val skew = props.clockSkewSeconds

            val exp = claims.expirationTime?.toInstant() ?: return null
            if (now.isAfter(exp.plusSeconds(skew))) return null

            claims.notBeforeTime?.toInstant()?.let { nbf ->
                if (now.plusSeconds(skew).isBefore(nbf)) return null
            }

            if (claims.issuer != props.issuerUrl) return null

            val aud = claims.audience ?: emptyList()
            if (requireAudience != "*" && !aud.contains(requireAudience) && !aud.contains("*")) return null

            jwt
        } catch (ex: Exception) {
            log.debug("token verification failed: {}", ex.message)
            null
        }
    }

    /** Result of a mint: the compact JWS plus the identifiers callers need for revocation/audit. */
    data class MintedToken(
        val token: String,
        val jti: UUID,
        val kid: UUID,
        val expiresAt: Instant,
    )

    private companion object {
        val log = LoggerFactory.getLogger(JwtMintService::class.java)
    }
}
