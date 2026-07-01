package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.revocation.*` configuration tree (WP03 — the SHARED
 * REVOCATION SCHEME all four services agree on). Bound by @ConfigurationPropertiesScan on
 * [ai.cypherx.auth.AuthApplication]. Every value is env-overridable — nothing here is a hardcoded
 * tunable; the in-code defaults are the documented fallbacks.
 *
 * Valkey keys (all under [keyPrefix], default `cypherx:rev:`):
 *  - `<prefix>jti:{jti}`        -> "1", TTL = token's remaining lifetime (revoke ONE token).
 *  - `<prefix>kid:{kid}`        -> "1", long TTL (poison a signing key on emergency rotate).
 *  - `<prefix>agent:{agent_id}` -> unix-epoch-seconds (revoke-all for an agent: every token with
 *                                  iat < that epoch is rejected).
 *
 * Verifier check (Auth verify path AND the mirror middleware in llms/guardrails/xagent), AFTER
 * signature/iss/aud/exp pass: reject 401 TOKEN_REVOKED if ANY of jti / kid / (agent epoch > iat).
 * FAIL-OPEN: a Valkey outage ACCEPTS the token (revocation is a defence-in-depth kill-switch —
 * availability wins) and increments a metric. A short [valkeyTimeoutMs] keeps a slow Valkey from
 * stalling a request.
 */
@ConfigurationProperties(prefix = "cypherx.auth.revocation")
data class RevocationProperties(

    /** Key namespace shared by every service's verifier. MUST match across services. */
    val keyPrefix: String = "cypherx:rev:",

    /** TTL (seconds) for a poisoned-kid key. Defaults to the max JWT TTL so it covers in-flight tokens. */
    val kidPoisonTtlSeconds: Long = 3600,

    /**
     * TTL (seconds) for an `agent:{id}` revoke-all epoch marker. Must outlive any token that could
     * carry an `iat` before the marker — default = max JWT TTL + 1h slack.
     */
    val agentEpochTtlSeconds: Long = 7200,

    /** Hard cap on a single Valkey revocation lookup so a slow cache never stalls the verify path. */
    val valkeyTimeoutMs: Long = 150,
) {
    fun jtiKey(jti: Any): String = "${keyPrefix}jti:$jti"
    fun kidKey(kid: Any): String = "${keyPrefix}kid:$kid"
    fun agentKey(agentId: Any): String = "${keyPrefix}agent:$agentId"
}
