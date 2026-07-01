package ai.cypherx.auth.service

import ai.cypherx.auth.config.RevocationProperties
import io.micrometer.core.instrument.MeterRegistry
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.ObjectProvider
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.stereotype.Component
import java.time.Instant

/**
 * Verifier-side revocation check (WP03 — the SHARED REVOCATION SCHEME). This is the read side that
 * Auth's own [ai.cypherx.auth.web.AgentJwtAuthFilter] runs AFTER signature/iss/aud/exp pass, and the
 * exact same logic the mirror middleware in llms/guardrails/xagent runs. [RevocationService] is the
 * WRITE side (it SETs the keys this class reads).
 *
 * A token is revoked if ANY of:
 *   - `<prefix>jti:{jti}` exists (that specific token was revoked), OR
 *   - `<prefix>kid:{kid}` exists (the signing key was poisoned / emergency-rotated), OR
 *   - `<prefix>agent:{agent_id}` exists AND token.iat < that epoch (revoke-all for the agent).
 *
 * FAIL-OPEN: if Valkey is unavailable (or the lookup exceeds [RevocationProperties.valkeyTimeoutMs]),
 * the token is ACCEPTED — revocation is a defence-in-depth kill-switch and availability wins. Every
 * skip logs `revocation_check_skipped=true` and increments the `auth_revocation_check_skipped_total`
 * metric so the fail-open is observable.
 */
@Component
class RevocationChecker(
    private val props: RevocationProperties,
    redisProvider: ObjectProvider<StringRedisTemplate>,
    meterRegistryProvider: ObjectProvider<MeterRegistry>,
) {

    /** null when Redis/Valkey auto-config is absent — the checker then always fails open. */
    private val redis: StringRedisTemplate? = redisProvider.ifAvailable
    private val meters: MeterRegistry? = meterRegistryProvider.ifAvailable

    /** Outcome of a check — explicit so callers can distinguish "revoked" from "fail-open accepted". */
    enum class Decision { ALLOWED, REVOKED }

    /**
     * @param jti  the token's `jti` (may be null — then only kid/agent apply).
     * @param kid  the token header `kid`.
     * @param agentId the token `agent_id` / `sub`.
     * @param issuedAt the token `iat` — compared against the agent revoke-all epoch.
     */
    fun check(jti: String?, kid: String?, agentId: String?, issuedAt: Instant?): Decision {
        val r = redis ?: run { skip("valkey-absent"); return Decision.ALLOWED }
        return try {
            // jti — exact-token revoke.
            if (jti != null && exists(r, props.jtiKey(jti))) return Decision.REVOKED
            // kid — poisoned signing key (emergency rotation).
            if (kid != null && exists(r, props.kidKey(kid))) return Decision.REVOKED
            // agent epoch — revoke-all: every token whose iat predates the marker is dead.
            if (agentId != null && issuedAt != null) {
                val epoch = get(r, props.agentKey(agentId))?.toLongOrNull()
                if (epoch != null && issuedAt.epochSecond < epoch) return Decision.REVOKED
            }
            Decision.ALLOWED
        } catch (ex: Exception) {
            // FAIL-OPEN — a Valkey error/timeout never rejects a token.
            skip(ex.message ?: ex.javaClass.simpleName)
            Decision.ALLOWED
        }
    }

    private fun exists(r: StringRedisTemplate, key: String): Boolean = r.hasKey(key)

    private fun get(r: StringRedisTemplate, key: String): String? = r.opsForValue().get(key)

    private fun skip(reason: String) {
        meters?.counter("auth_revocation_check_skipped_total")?.increment()
        log.warn("revocation_check_skipped=true reason={}", reason)
    }

    private companion object {
        val log = LoggerFactory.getLogger(RevocationChecker::class.java)
    }
}
