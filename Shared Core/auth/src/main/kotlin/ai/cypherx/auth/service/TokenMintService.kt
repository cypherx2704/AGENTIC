package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.AgentStatus
import ai.cypherx.auth.domain.ApiKeyStatus
import ai.cypherx.auth.repo.ApiKeyRepository
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.http.HttpStatus
import org.springframework.stereotype.Service
import java.security.MessageDigest
import java.sql.ResultSet
import java.time.Duration
import java.time.Instant
import java.util.UUID

/**
 * Exchanges a raw API key for a short-lived agent JWT (Component 3 — `POST /v1/agents/{id}/token`).
 *
 * Flow (Phase 2 §Component 3):
 *   1. SHA-256 the presented api_key, look it up within the tenant (RLS).
 *   2. Verify the key is ACTIVE and not expired, and that it belongs to the path agent.
 *   3. Load the agent; verify it is ACTIVE.
 *   4. effectiveScopes = key.scopes ∩ agent.allowed_scopes ∩ requested (requested omitted ⇒
 *      key.scopes ∩ agent.allowed_scopes). If any requested scope is filtered out → 403 with the
 *      offending scope(s) so clients can diagnose.
 *   5. Mint the Contract 1 agent token via [JwtMintService] (1h TTL) with optional claims
 *      plan / region / agent_version / api_key_id.
 *   6. Best-effort: record the jti in Valkey `agent-active-jtis:{agent_id}` (for revoke-all-tokens)
 *      and stamp last_used_at on the key.
 *
 * The token endpoint is permit-all and body-authenticates via the api_key. The tenant is resolved
 * from the caller-supplied `X-Tenant-ID` (Kong / SDK) — never from the body (Contract 13). RLS
 * makes a key/agent lookup impossible without a tenant context, so the tenant is mandatory.
 */
@Service
class TokenMintService(
    private val apiKeyRepository: ApiKeyRepository,
    private val jwtMintService: JwtMintService,
    private val tenantTx: TenantTx,
    private val props: AuthProperties,
    private val objectMapper: ObjectMapper,
    private val quotaService: QuotaService,
    private val auditService: AuditService,
) {

    /** Optional — present only when spring-data-redis is configured. jti tracking degrades gracefully. */
    @Autowired(required = false)
    private var redis: StringRedisTemplate? = null

    /** Result of a successful exchange. */
    data class MintedAccessToken(
        val token: String,
        val tokenType: String = "Bearer",
        val expiresIn: Long,
        val scopes: List<String>,
    )

    private data class AgentRow(
        val agentId: UUID,
        val status: String,
        val version: String,
        val allowedScopes: List<String>,
        val plan: String?,
        val region: String?,
        val agentType: String,
        val parentOrchestratorId: UUID?,
    )

    /**
     * Exchange [rawApiKey] for an agent JWT scoped to [agentId] in [tenantId].
     *
     * @param requestedScopes scopes the caller wants; when empty the full granted intersection
     *        (key ∩ agent) is issued.
     */
    fun exchange(
        tenantId: UUID,
        agentId: UUID,
        rawApiKey: String,
        requestedScopes: List<String>,
    ): MintedAccessToken {
        if (rawApiKey.isBlank()) {
            throw ApiException.unauthorized("Missing api_key", mapOf("field" to "api_key"))
        }

        val keyHash = sha256Hex(rawApiKey.trim())
        val key = apiKeyRepository.findByHash(tenantId, keyHash)
            ?: throw ApiException.unauthorized("Invalid API key")

        // Bind the key to the path agent (defence against cross-agent key replay within a tenant).
        if (key.agentId != agentId) {
            throw ApiException.forbidden(
                "API key does not belong to this agent",
                mapOf("agent_id" to agentId.toString()),
            )
        }

        // Status / expiry checks.
        when (key.status) {
            ApiKeyStatus.REVOKED.value ->
                throw ApiException.unauthorized("API key has been revoked")
            ApiKeyStatus.EXPIRED.value ->
                throw ApiException.unauthorized("API key has expired")
        }
        if (key.expiresAt != null && Instant.now().isAfter(key.expiresAt)) {
            throw ApiException.unauthorized("API key has expired")
        }

        val agent = loadAgent(tenantId, agentId)
            ?: throw ApiException.notFound("Agent not found", mapOf("agent_id" to agentId.toString()))
        if (agent.status != AgentStatus.ACTIVE.value) {
            throw ApiException.forbidden(
                "Agent is not active",
                mapOf("agent_id" to agentId.toString(), "status" to agent.status),
            )
        }

        // ── Quota: tenant plan + tokens-issued-per-min (Component 1d / Contract 19) ──────────
        // Resolve the tenant's effective limits ONCE: enforce the auth token-issuance rate and
        // carry the TENANT plan in the minted token (downstream services key quota off it). Quota
        // RESOLUTION fails open (a quota-subsystem hiccup must not block legitimate issuance); only
        // a genuine over-limit COUNT rejects with 429.
        val resolution = runCatching { quotaService.resolve(tenantId) }
            .onFailure { log.debug("quota resolve skipped for tenant {}: {}", tenantId, it.message) }
            .getOrNull()
        if (resolution != null) {
            val tokensPerMin = resolution.effective.path("auth").path("tokens_issued_per_min").asInt(0)
            enforceTokenRateQuota(tenantId, tokensPerMin)
        }
        val tenantPlan: String? = resolution?.plan

        val cleanRequested = requestedScopes.map { it.trim() }.filter { it.isNotEmpty() }.distinct()
        val granted = key.scopes.toSet().intersect(agent.allowedScopes.toSet())

        val effective: List<String> = if (cleanRequested.isEmpty()) {
            granted.toList()
        } else {
            val missing = cleanRequested.filter { it !in granted }
            if (missing.isNotEmpty()) {
                throw ApiException.forbidden(
                    "Requested scope(s) not granted by API key and agent",
                    mapOf("missing" to missing),
                )
            }
            cleanRequested
        }

        if (effective.isEmpty()) {
            throw ApiException.forbidden(
                "No effective scopes (key ∩ agent allowed_scopes is empty)",
                mapOf(
                    "key_scopes" to key.scopes,
                    "agent_allowed_scopes" to agent.allowedScopes,
                ),
            )
        }

        val extra = buildMap<String, Any?> {
            put("api_key_id", key.keyId.toString())
            agent.version.let { put("agent_version", it) }
            // Prefer the resolved TENANT plan (Contract 19 quota tier); fall back to the agent's
            // metadata plan only if quota resolution was unavailable.
            (tenantPlan ?: agent.plan)?.let { put("plan", it) }
            agent.region?.let { put("region", it) }
            // Orchestrator hierarchy claims — let downstream services (xAgent, llms, tools) enforce
            // agent-type rules without an extra Auth round-trip (Contract 1 forward-compat: optional).
            put("agent_type", agent.agentType)
            agent.parentOrchestratorId?.let { put("parent_orchestrator_id", it.toString()) }
        }

        val minted = jwtMintService.mintAgentToken(
            agentId = agentId,
            tenantId = tenantId,
            scopes = effective,
            ttlSeconds = props.agentTokenTtlSeconds,
            extraClaims = extra,
        )

        recordActiveJti(agentId, minted.jti, minted.expiresAt)
        runCatching { apiKeyRepository.touchLastUsed(tenantId, key.keyId) }
            .onFailure { log.debug("touchLastUsed failed for {}: {}", key.keyId, it.message) }

        // Durable audit (Component 6 — issuance-event coverage). Best-effort; never blocks issuance.
        runCatching {
            auditService.record(
                eventType = "token.issued",
                tenantId = tenantId,
                agentId = agentId,
                action = "token:issue",
                resource = "jti:${minted.jti}",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for token.issued jti {}: {}", minted.jti, it.message) }

        val expiresIn = Duration.between(Instant.now(), minted.expiresAt).seconds.coerceAtLeast(1)
        return MintedAccessToken(
            token = minted.token,
            expiresIn = expiresIn,
            scopes = effective,
        )
    }

    // ── helpers ───────────────────────────────────────────────────────────────────────────

    /**
     * Minimal tenant-scoped agent read (status / version / allowed_scopes + optional plan & region
     * from metadata). Done inline here rather than via an agent-registry repository owned by another
     * feature, to keep this feature self-contained.
     */
    private fun loadAgent(tenantId: UUID, agentId: UUID): AgentRow? =
        tenantTx.inTenant(tenantId) { jdbc ->
            jdbc.query(
                """
                SELECT agent_id, status, version, allowed_scopes, metadata,
                       agent_type, parent_orchestrator_id
                  FROM auth.agents
                 WHERE agent_id = ?
                """.trimIndent(),
                { rs: ResultSet, _: Int ->
                    AgentRow(
                        agentId = rs.getObject("agent_id", UUID::class.java),
                        status = rs.getString("status"),
                        version = rs.getString("version"),
                        allowedScopes = readTextArray(rs, "allowed_scopes"),
                        plan = readMetadataString(rs.getString("metadata"), "plan"),
                        region = readMetadataString(rs.getString("metadata"), "region"),
                        agentType = rs.getString("agent_type") ?: "user_created",
                        parentOrchestratorId = rs.getObject("parent_orchestrator_id", UUID::class.java),
                    )
                },
                agentId,
            ).firstOrNull()
        }

    private fun readMetadataString(json: String?, field: String): String? {
        if (json.isNullOrBlank()) return null
        return runCatching {
            val node = objectMapper.readTree(json)
            node.get(field)?.takeIf { it.isValueNode }?.asText()?.takeIf { it.isNotBlank() }
        }.getOrNull()
    }

    /**
     * SADD the jti to `agent-active-jtis:{agent_id}` and bump the set TTL so revoke-all-tokens can
     * enumerate live jtis (Component 3c). Best-effort: a Valkey outage must not block token issuance
     * (fail-open with a debug log).
     */
    private fun recordActiveJti(agentId: UUID, jti: UUID, expiresAt: Instant) {
        val r = redis ?: return
        runCatching {
            val key = "agent-active-jtis:$agentId"
            r.opsForSet().add(key, jti.toString())
            val ttl = Duration.between(Instant.now(), expiresAt).coerceAtLeast(Duration.ofSeconds(1))
            val existing = r.getExpire(key)
            if (existing == null || existing < ttl.seconds) {
                r.expire(key, ttl)
            }
        }.onFailure { log.debug("recordActiveJti skipped (valkey unavailable): {}", it.message) }
    }

    /**
     * Per-tenant fixed-window (1-minute) token-issuance quota (Contract 19 `auth.tokens_issued_per_min`).
     * A limit <= 0 (or absent) means unlimited. FAIL-OPEN: no Valkey, or any counter error, never
     * blocks issuance — only a real over-limit count throws 429 QUOTA_EXCEEDED.
     */
    private fun enforceTokenRateQuota(tenantId: UUID, tokensPerMin: Int) {
        if (tokensPerMin <= 0) return // unlimited / not configured
        val r = redis ?: return // fail-open: no Valkey wired -> no enforcement
        val count = runCatching {
            val windowMin = Instant.now().epochSecond / 60
            val key = "cypherx:auth:quota:tokens:$tenantId:$windowMin"
            val c = r.opsForValue().increment(key) ?: 1L
            if (c == 1L) r.expire(key, Duration.ofSeconds(60))
            c
        }.getOrElse {
            log.debug("token-rate quota counter skipped (valkey unavailable): {}", it.message)
            return // fail-open
        }
        if (count > tokensPerMin) {
            throw ApiException(
                "QUOTA_EXCEEDED",
                HttpStatus.TOO_MANY_REQUESTS,
                "Token issuance quota exceeded for this minute",
                mapOf("limit_per_min" to tokensPerMin, "scope" to "auth.tokens_issued_per_min"),
            )
        }
    }

    private fun sha256Hex(raw: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(raw.toByteArray(Charsets.UTF_8))
        return digest.joinToString("") { "%02x".format(it) }
    }

    private fun readTextArray(rs: ResultSet, column: String): List<String> {
        val arr = rs.getArray(column) ?: return emptyList()
        @Suppress("UNCHECKED_CAST")
        val raw = arr.array as? Array<Any?> ?: return emptyList()
        return raw.filterNotNull().map { it.toString() }
    }

    private companion object {
        val log = LoggerFactory.getLogger(TokenMintService::class.java)
    }
}
