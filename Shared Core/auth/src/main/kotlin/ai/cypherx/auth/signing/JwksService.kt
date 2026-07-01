package ai.cypherx.auth.signing

import com.nimbusds.jose.jwk.JWKSet
import org.springframework.stereotype.Service

/**
 * Builds the public JWKS document served at `/.well-known/jwks.json` (Contract 1 §3) from the
 * signing + verifying public keys held by [SigningKeyService].
 *
 * Output shape: `{ "keys": [ { kty, kid, use, alg, n, e }, ... ] }`. Only PUBLIC parameters are
 * exported (Nimbus's [JWKSet.toJSONObject] / `toPublicJWKSet` already strips private params; we
 * pass public-only keys regardless).
 */
@Service
class JwksService(private val signingKeyService: SigningKeyService) {

    /** The JWKS as a `Map` Spring serializes to JSON. Safe to expose publicly. */
    fun jwksJson(): Map<String, Any> {
        val keys = signingKeyService.verifiers()        // already public-only RSAKeys
        val set = JWKSet(keys).toPublicJWKSet()
        // includePrivateParameters=false → public JWKS document.
        return set.toJSONObject(false)
    }
}
