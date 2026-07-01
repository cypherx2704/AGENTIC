package ai.cypherx.auth.service

import ai.cypherx.auth.config.RevocationProperties
import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.AgentStatus
import ai.cypherx.auth.domain.RevocationReason
import ai.cypherx.auth.kafka.AuthEventPublisher
import ai.cypherx.auth.kafka.OutboxEventWriter
import ai.cypherx.auth.repo.RevokedTokenRepository
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.ObjectProvider
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.stereotype.Service
import java.time.Duration
import java.time.Instant
import java.util.UUID

/**
 * Live token revocation (Component 3c, Phase 2).
 *
 * Revoking a `jti` has three durable/eventual effects, applied in this order so the system-of-record
 * is written before anything observable fans out:
 *
 *  1. INSERT `auth.revoked_tokens` AND the `cypherx.auth.token.revoked` outbox row in ONE
 *     transaction (publication guarantee, amended 2026-06 — the [ai.cypherx.auth.kafka.OutboxRelay]
 *     publishes so every verifier primes its in-process bloom filter; no log-and-drop).
 *  2. SET `<prefix>jti:{jti}` in Valkey (shared scheme, default prefix `cypherx:rev:`) with TTL =
 *     remaining token lifetime (the hot-path deny check every verifier performs; auto-expires when
 *     the token would naturally die). revoke-all additionally writes `<prefix>agent:{agent_id}` =
 *     epoch-seconds, and emergency key rotation writes `<prefix>kid:{kid}`.
 *
 * Plus an `auth.audit_log` row (`event_type = token.revoked`) per the Component 6 audit requirement.
 *
 * Resilience: Valkey is a best-effort side-effect. If Valkey is down the DB row + outbox-relayed
 * Kafka event still propagate the revocation (verifiers fall back to the durable list / replay);
 * failures are logged at WARN and never abort the revoke (the durable DB write already succeeded).
 *
 * `revoke-all-tokens` reads the per-agent active-jti set `agent-active-jtis:{agent_id}` (populated
 * at token issuance) from Valkey, revokes each, and marks the agent `suspended` so no new tokens
 * are minted (phase doc Component 3c).
 */
@Service
class RevocationService(
    private val revokedTokenRepository: RevokedTokenRepository,
    private val auditService: AuditService,
    private val eventPublisher: AuthEventPublisher,
    private val outboxEvents: OutboxEventWriter,
    private val tenantTx: TenantTx,
    private val revocationProps: RevocationProperties,
    redisProvider: ObjectProvider<StringRedisTemplate>,
) {

    /** null when Redis/Valkey auto-config is absent — the service then runs DB+Kafka only. */
    private val redis: StringRedisTemplate? = redisProvider.ifAvailable

    /** Outcome surfaced to the controller for the single-jti path. */
    data class RevokeResult(val jti: UUID, val alreadyRevoked: Boolean)

    /**
     * Revoke a single [jti]. The token's tenant/agent/exp are resolved from the supplied claims (the
     * caller extracts them from the presented/looked-up token). [tokenExp] bounds the Valkey TTL and
     * the durable row's purge time.
     *
     * @param revokedBy the acting principal (admin agent id or px0 user id).
     */
    fun revokeJti(
        jti: UUID,
        tenantId: UUID,
        agentId: UUID?,
        reason: RevocationReason,
        revokedBy: UUID,
        tokenExp: Instant,
    ): RevokeResult {
        val now = Instant.now()
        // Durable revocation row + `cypherx.auth.token.revoked` outbox row in ONE transaction
        // (publication guarantee). The outbox row is written only when the revocation is NEW —
        // a re-revoked jti changes no state, so it re-emits nothing.
        val inserted = tenantTx.inPlatform { jdbc ->
            val newRow = revokedTokenRepository.insert(
                jti = jti,
                agentId = agentId,
                tenantId = tenantId,
                revokedBy = revokedBy,
                reason = reason.value,
                tokenExp = tokenExp,
                revokedAt = now,
            )
            if (newRow) {
                outboxEvents.tokenRevoked(
                    jdbc,
                    jti = jti,
                    tenantId = tenantId,
                    agentId = agentId,
                    reason = reason.value,
                    tokenExp = tokenExp,
                    revokedAt = now,
                )
            }
            newRow
        }

        // Hot-path deny entry — TTL only as long as the token could still be presented.
        setRevokedInValkey(jti, tokenExp, now)

        // Durable audit (Component 6).
        runCatching {
            auditService.record(
                eventType = "token.revoked",
                tenantId = tenantId,
                agentId = agentId,
                action = "token:revoke",
                resource = "jti:$jti",
                decision = "allow",
                createdAt = now,
            )
        }.onFailure { log.warn("audit write failed for token.revoke jti {}: {}", jti, it.message) }

        return RevokeResult(jti = jti, alreadyRevoked = !inserted)
    }

    /**
     * Revoke every live token for [agentId] and suspend the agent so no new tokens are minted.
     *
     * Live jtis come from the Valkey set `agent-active-jtis:{agent_id}` populated at issuance. When
     * Valkey is unavailable (or the set is empty) we cannot enumerate individual jtis — we still
     * suspend the agent (blocking new tokens) and rely on the agent being `suspended` plus natural
     * token expiry (<=1h). Returns the count of jtis revoked.
     */
    fun revokeAllForAgent(
        agentId: UUID,
        tenantId: UUID,
        reason: RevocationReason,
        revokedBy: UUID,
        defaultTokenTtl: Duration,
    ): Int {
        val now = Instant.now()
        val fallbackExp = now.plus(defaultTokenTtl)
        val liveJtis = readAgentActiveJtis(agentId)

        // SHARED REVOCATION SCHEME — write the `agent:{id}` epoch marker FIRST: every token whose
        // `iat` predates `now` is rejected verifier-side even if we cannot enumerate its jti (Valkey
        // set empty / Valkey down). This is the durable cascade kill-switch; the per-jti SETs below
        // are the precise complement when the live-set is available.
        setAgentEpochInValkey(agentId, now)

        var revoked = 0
        for (raw in liveJtis) {
            val jti = runCatching { UUID.fromString(raw) }.getOrNull() ?: continue
            // We do not have each token's exact exp here; bound the deny TTL by the max agent TTL.
            revokeJti(
                jti = jti,
                tenantId = tenantId,
                agentId = agentId,
                reason = reason,
                revokedBy = revokedBy,
                tokenExp = fallbackExp,
            )
            revoked++
        }

        // Block future token issuance: mark the agent suspended.
        suspendAgent(agentId, tenantId)

        // Emit the compact agent-state event so caches reflect the new status (advisory topic
        // cypherx.auth.agent.updated — best-effort direct publish).
        runCatching {
            eventPublisher.agentUpdated(agentId = agentId, tenantId = tenantId, status = AgentStatus.SUSPENDED.value, updatedAt = now)
        }.onFailure { log.warn("agent.updated publish failed for agent {}: {}", agentId, it.message) }

        runCatching {
            auditService.record(
                eventType = "agent.revoke_all",
                tenantId = tenantId,
                agentId = agentId,
                action = "token:revoke-all",
                resource = "agent:$agentId",
                decision = "allow",
                createdAt = now,
            )
        }.onFailure { log.warn("audit write failed for revoke-all agent {}: {}", agentId, it.message) }

        // Clear the now-stale active-jti set (best effort).
        runCatching { redis?.delete(agentActiveSetKey(agentId)) }

        return revoked
    }

    // ── Valkey helpers (all best-effort) ───────────────────────────────────────────────────

    private fun setRevokedInValkey(jti: UUID, tokenExp: Instant, now: Instant) {
        val r = redis ?: return
        val ttl = Duration.between(now, tokenExp)
        if (ttl.isNegative || ttl.isZero) return // already expired; nothing to deny
        // SHARED REVOCATION SCHEME — write the namespaced key every verifier (auth's own
        // RevocationChecker + the llms/guardrails/xagent mirrors) reads: `<prefix>jti:{jti}`.
        runCatching { r.opsForValue().set(revocationProps.jtiKey(jti), "1", ttl) }
            .onFailure { log.warn("valkey SET {} failed: {}", revocationProps.jtiKey(jti), it.message) }
    }

    /**
     * SHARED REVOCATION SCHEME — revoke-all cutoff for an agent. Writes `<prefix>agent:{agent_id}` =
     * the unix-epoch-seconds of [now]; every verifier rejects any token for that agent whose `iat`
     * predates this marker. TTL = [RevocationProperties.agentEpochTtlSeconds] (outlives the longest
     * token that could carry an earlier iat). Best-effort (the durable DB suspend is the SoR).
     */
    private fun setAgentEpochInValkey(agentId: UUID, now: Instant) {
        val r = redis ?: return
        val ttl = Duration.ofSeconds(revocationProps.agentEpochTtlSeconds)
        runCatching { r.opsForValue().set(revocationProps.agentKey(agentId), now.epochSecond.toString(), ttl) }
            .onFailure { log.warn("valkey SET {} failed: {}", revocationProps.agentKey(agentId), it.message) }
    }

    /**
     * SHARED REVOCATION SCHEME — poison a signing `kid` (emergency key rotation). Writes
     * `<prefix>kid:{kid}` so every verifier rejects ANY token signed by that key, regardless of jti.
     * TTL = [RevocationProperties.kidPoisonTtlSeconds] (covers the longest in-flight token). Called by
     * the key-rotation service's emergency path; best-effort (the rotated JWKS is the durable SoR).
     */
    fun poisonKid(kid: String) {
        val r = redis ?: return
        val ttl = Duration.ofSeconds(revocationProps.kidPoisonTtlSeconds)
        runCatching { r.opsForValue().set(revocationProps.kidKey(kid), "1", ttl) }
            .onFailure { log.warn("valkey SET {} failed: {}", revocationProps.kidKey(kid), it.message) }
    }

    private fun readAgentActiveJtis(agentId: UUID): Set<String> {
        val r = redis ?: return emptySet()
        return runCatching { r.opsForSet().members(agentActiveSetKey(agentId)) ?: emptySet() }
            .onFailure { log.warn("valkey SMEMBERS agent-active-jtis:{} failed: {}", agentId, it.message) }
            .getOrDefault(emptySet())
    }

    /** Mark the agent suspended (tenant-scoped). No-op effect if the row does not exist. */
    private fun suspendAgent(agentId: UUID, tenantId: UUID) {
        runCatching {
            tenantTx.inTenant(tenantId) { jdbc ->
                jdbc.update(
                    "UPDATE auth.agents SET status = ?, updated_at = NOW() WHERE agent_id = ?",
                    AgentStatus.SUSPENDED.value,
                    agentId,
                )
            }
        }.onFailure { log.warn("failed to suspend agent {}: {}", agentId, it.message) }
    }

    companion object {
        // Auth-internal operational set (NOT part of the shared verifier scheme): the live jtis for
        // an agent, populated at token issuance and read by revoke-all to deny each precisely. The
        // shared cross-service kill-switch keys live in RevocationProperties (jtiKey/kidKey/agentKey).
        fun agentActiveSetKey(agentId: UUID) = "agent-active-jtis:$agentId"
        private val log = LoggerFactory.getLogger(RevocationService::class.java)
    }
}
