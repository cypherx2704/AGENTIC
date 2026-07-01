package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.ApiKeyStatus
import ai.cypherx.auth.domain.SYSTEM_USER_ID
import ai.cypherx.auth.repo.AgentRepository
import ai.cypherx.auth.repo.ApiKeyRepository
import ai.cypherx.auth.web.ApiException
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.security.MessageDigest
import java.security.SecureRandom
import java.time.Duration
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.Base64
import java.util.UUID

/**
 * Issues, lists and revokes agent API keys (Component 2 / Contract 18 key format).
 *
 * Key format (Phase 2 task): `cx_<env>_<base64url(32 random bytes)>`. We store only the SHA-256 hex
 * of the full raw key plus an 8-char display prefix — the raw key is returned EXACTLY ONCE at
 * creation and is unrecoverable thereafter.
 *
 * All persistence is tenant-scoped via [ApiKeyRepository] (RLS). The caller supplies the resolved
 * tenant (from the agent JWT / X-Tenant-ID); this service never trusts a tenant id from a request
 * body (Contract 13 anti-pattern).
 */
@Service
class ApiKeyService(
    private val apiKeyRepository: ApiKeyRepository,
    private val agentRepository: AgentRepository,
    private val props: AuthProperties,
    private val quotaService: QuotaService,
    private val auditService: AuditService,
) {

    private val rng = SecureRandom()

    /** Result of issuing a key — the ONLY time the raw secret is exposed. */
    data class IssuedKey(
        val keyId: UUID,
        val rawKey: String,
        val keyPrefix: String,
        val scopes: List<String>,
        val expiresAt: Instant?,
        val createdAt: Instant,
    )

    /**
     * Result of a rotation: the freshly-minted [newKey] (raw secret shown once) plus the id of the
     * previous key and the instant its 24h dual-validity grace ends (until then it is still usable).
     */
    data class RotatedKey(
        val newKey: IssuedKey,
        val previousKeyId: UUID,
        val previousKeyExpiresAt: Instant,
    )

    /** A key as shown in list responses — never carries the secret. */
    data class KeyView(
        val keyId: UUID,
        val agentId: UUID,
        val keyPrefix: String,
        val name: String?,
        val scopes: List<String>,
        val status: String,
        val expiresAt: Instant?,
        val lastUsedAt: Instant?,
        val createdAt: Instant,
        val revokedAt: Instant?,
    )

    /**
     * Generate and persist a new API key for [agentId] in [tenantId].
     *
     * @param scopes the scopes the key may later exchange for (upper bound at /token time).
     * @param name   optional human label.
     * @param expiresInDays optional expiry; when null the key never expires (discouraged for prod).
     * @return the [IssuedKey] containing the raw secret (shown once).
     */
    fun issue(
        tenantId: UUID,
        agentId: UUID,
        scopes: List<String>,
        name: String?,
        expiresInDays: Long?,
    ): IssuedKey {
        if (expiresInDays != null && expiresInDays <= 0) {
            throw ApiException.validation(
                "expires_in_days must be positive",
                mapOf("field" to "expires_in_days", "value" to expiresInDays),
            )
        }

        enforceApiKeysPerAgentQuota(tenantId, agentId)

        val cleanScopes = scopes.map { it.trim() }.filter { it.isNotEmpty() }.distinct()
        validateScopesAgainstAgent(tenantId, agentId, cleanScopes)

        val rawKey = generateRawKey()
        val keyHash = sha256Hex(rawKey)
        val keyPrefix = rawKey.take(PREFIX_LEN)
        val expiresAt = expiresInDays?.let { Instant.now().plus(it, ChronoUnit.DAYS) }

        val keyId = apiKeyRepository.insert(
            tenantId = tenantId,
            agentId = agentId,
            keyHash = keyHash,
            keyPrefix = keyPrefix,
            name = name,
            scopes = cleanScopes,
            expiresAt = expiresAt,
        )

        // Durable audit (Component 6 — issuance-event coverage). Best-effort; never blocks issuance.
        runCatching {
            auditService.record(
                eventType = "api_key.created",
                tenantId = tenantId,
                agentId = agentId,
                action = "api_key:create",
                resource = "api_key:$keyId",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for api_key.created {}: {}", keyId, it.message) }

        return IssuedKey(
            keyId = keyId,
            rawKey = rawKey,
            keyPrefix = keyPrefix,
            scopes = cleanScopes,
            expiresAt = expiresAt,
            createdAt = Instant.now(),
        )
    }

    /**
     * Rotate [keyId] for [agentId] in [tenantId]: mint a fresh key (the raw secret returned ONCE)
     * while keeping the OLD key valid for a 24h DUAL-VALIDITY grace.
     *
     * Grace semantics: the old key's `status` stays `active` and its `expires_at` is set to
     * `now + 24h` (never EXTENDED if it already expires sooner). Both keys therefore exchange for
     * tokens until the grace elapses, after which the token-exchange path rejects the old key on the
     * `now > expires_at` check ([TokenMintService]) — no background sweep is required. After 24h the
     * caller may also explicitly [revoke] the old key.
     *
     * The new key inherits the old key's scopes (and name, suffixed) unless overridden. Errors:
     *  - 404 NOT_FOUND       — key unknown, or belongs to a different agent.
     *  - 409 CONFLICT        — the old key is revoked or already expired (nothing to rotate).
     * Quota is NOT re-checked here: a rotation is net-neutral on the active-key count (the old key
     * stays active during the grace, but rotation is an explicit replace, not a fan-out).
     *
     * @param graceDuration how long the old key remains valid; defaults to 24h.
     */
    fun rotate(
        tenantId: UUID,
        agentId: UUID,
        keyId: UUID,
        scopesOverride: List<String>? = null,
        nameOverride: String? = null,
        expiresInDays: Long? = null,
        rotatedBy: UUID = SYSTEM_USER_ID,
        graceDuration: Duration = DEFAULT_ROTATION_GRACE,
    ): RotatedKey {
        if (expiresInDays != null && expiresInDays <= 0) {
            throw ApiException.validation(
                "expires_in_days must be positive",
                mapOf("field" to "expires_in_days", "value" to expiresInDays),
            )
        }

        val existing = apiKeyRepository.findById(tenantId, keyId)
            ?: throw ApiException.notFound("API key not found", mapOf("key_id" to keyId.toString()))
        if (existing.agentId != agentId) {
            throw ApiException.notFound(
                "API key not found for this agent",
                mapOf("key_id" to keyId.toString(), "agent_id" to agentId.toString()),
            )
        }
        // Only an active, unexpired key can be rotated — a revoked/expired key has nothing to grace.
        if (existing.status == ApiKeyStatus.REVOKED.value) {
            throw ApiException.conflict(
                "Cannot rotate a revoked API key",
                mapOf("key_id" to keyId.toString(), "status" to existing.status),
            )
        }
        if (existing.status == ApiKeyStatus.EXPIRED.value ||
            (existing.expiresAt != null && Instant.now().isAfter(existing.expiresAt))
        ) {
            throw ApiException.conflict(
                "Cannot rotate an expired API key",
                mapOf("key_id" to keyId.toString(), "status" to existing.status),
            )
        }

        val cleanScopes = (scopesOverride ?: existing.scopes)
            .map { it.trim() }.filter { it.isNotEmpty() }.distinct()
        if (cleanScopes.isEmpty()) {
            throw ApiException.validation(
                "scopes must contain at least one non-empty scope",
                mapOf("field" to "scopes"),
            )
        }
        if (scopesOverride != null) validateScopesAgainstAgent(tenantId, agentId, cleanScopes)
        val newName = nameOverride?.takeIf { it.isNotBlank() }
            ?: existing.name?.let { "$it (rotated)" }

        // 1. Mint the NEW key (active immediately).
        val rawKey = generateRawKey()
        val keyHash = sha256Hex(rawKey)
        val keyPrefix = rawKey.take(PREFIX_LEN)
        val expiresAt = expiresInDays?.let { Instant.now().plus(it, ChronoUnit.DAYS) }
        val newKeyId = apiKeyRepository.insert(
            tenantId = tenantId,
            agentId = agentId,
            keyHash = keyHash,
            keyPrefix = keyPrefix,
            name = newName,
            scopes = cleanScopes,
            expiresAt = expiresAt,
        )

        // 2. Start the OLD key's grace window: stays active, expires_at = now + grace (never extended).
        val graceUntil = Instant.now().plus(graceDuration)
        apiKeyRepository.expireAt(tenantId, keyId, graceUntil)

        // Durable audit (Component 6). Best-effort; never blocks the rotation.
        runCatching {
            auditService.record(
                eventType = "api_key.rotated",
                tenantId = tenantId,
                agentId = agentId,
                action = "api_key:rotate",
                resource = "api_key:$newKeyId",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for api_key.rotated {}->{}: {}", keyId, newKeyId, it.message) }

        log.info("rotated api key {} -> {} for agent {} (old key valid until {})", keyId, newKeyId, agentId, graceUntil)
        return RotatedKey(
            newKey = IssuedKey(
                keyId = newKeyId,
                rawKey = rawKey,
                keyPrefix = keyPrefix,
                scopes = cleanScopes,
                expiresAt = expiresAt,
                createdAt = Instant.now(),
            ),
            previousKeyId = keyId,
            previousKeyExpiresAt = graceUntil,
        )
    }

    /** List the keys belonging to [agentId] in [tenantId] (no secrets). */
    fun list(tenantId: UUID, agentId: UUID): List<KeyView> =
        apiKeyRepository.listByAgent(tenantId, agentId).map {
            KeyView(
                keyId = it.keyId,
                agentId = it.agentId,
                keyPrefix = it.keyPrefix,
                name = it.name,
                scopes = it.scopes,
                status = it.status,
                expiresAt = it.expiresAt,
                lastUsedAt = it.lastUsedAt,
                createdAt = it.createdAt,
                revokedAt = it.revokedAt,
            )
        }

    /**
     * Revoke [keyId] for [agentId] in [tenantId]. 404 if the key is unknown or belongs to a
     * different agent; idempotent if already revoked (still 204 — no-op).
     */
    fun revoke(tenantId: UUID, agentId: UUID, keyId: UUID, revokedBy: UUID = SYSTEM_USER_ID) {
        val existing = apiKeyRepository.findById(tenantId, keyId)
            ?: throw ApiException.notFound(
                "API key not found",
                mapOf("key_id" to keyId.toString()),
            )
        if (existing.agentId != agentId) {
            throw ApiException.notFound(
                "API key not found for this agent",
                mapOf("key_id" to keyId.toString(), "agent_id" to agentId.toString()),
            )
        }
        if (existing.status == ApiKeyStatus.REVOKED.value) return
        apiKeyRepository.revoke(tenantId, keyId, revokedBy)

        // Durable audit (Component 6 — issuance-event coverage). Best-effort; never blocks revoke.
        runCatching {
            auditService.record(
                eventType = "api_key.revoked",
                tenantId = tenantId,
                agentId = agentId,
                action = "api_key:revoke",
                resource = "api_key:$keyId",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for api_key.revoked {}: {}", keyId, it.message) }
    }

    /**
     * Revoke ALL of [agentId]'s keys in [tenantId] in one statement (agent-deactivate cascade).
     * Returns the number of keys revoked. Audits a single `api_key.revoked` cascade event when any
     * key was revoked. Idempotent: a second call revokes nothing and returns 0. Best-effort audit.
     */
    fun revokeAllForAgent(tenantId: UUID, agentId: UUID, revokedBy: UUID = SYSTEM_USER_ID): Int {
        val revoked = apiKeyRepository.revokeAllByAgent(tenantId, agentId, revokedBy)
        if (revoked > 0) {
            runCatching {
                auditService.record(
                    eventType = "api_key.revoked",
                    tenantId = tenantId,
                    agentId = agentId,
                    action = "api_key:revoke-all",
                    resource = "agent:$agentId",
                    decision = "allow",
                )
            }.onFailure { log.warn("audit write failed for api_key.revoke-all agent {}: {}", agentId, it.message) }
        }
        return revoked
    }

    // ── helpers ───────────────────────────────────────────────────────────────────────────

    /**
     * Reject any scope in [requestedScopes] that is not in the agent's [AgentRecord.allowedScopes].
     * Mirrors the intersection enforced by [TokenMintService] at token-exchange time, but enforces it
     * earlier — at key issuance — so a persisted key can never carry scopes the agent does not hold.
     */
    private fun validateScopesAgainstAgent(tenantId: UUID, agentId: UUID, requestedScopes: List<String>) {
        val agent = agentRepository.findById(tenantId, agentId)
            ?: throw ApiException.notFound("Agent not found", mapOf("agent_id" to agentId.toString()))
        val disallowed = requestedScopes.filter { it !in agent.allowedScopes }
        if (disallowed.isNotEmpty()) {
            throw ApiException.forbidden(
                "Requested scope(s) not in agent's allowed_scopes",
                mapOf("disallowed" to disallowed, "allowed" to agent.allowedScopes),
            )
        }
    }

    /**
     * Enforce the Contract-19 `auth.api_keys_per_agent_max` quota (active keys per agent). A limit
     * <= 0 / absent means unlimited. Quota RESOLUTION fails open (a quota glitch must not block key
     * issuance); only a real at-or-over-limit count rejects with 409 QUOTA_EXCEEDED.
     */
    private fun enforceApiKeysPerAgentQuota(tenantId: UUID, agentId: UUID) {
        val limit = runCatching {
            quotaService.effectiveLimits(tenantId).path("auth").path("api_keys_per_agent_max").asInt(0)
        }.getOrElse { return } // fail-open on quota-subsystem trouble
        if (limit <= 0) return // unlimited / not configured
        val current = apiKeyRepository.countActiveByAgent(tenantId, agentId)
        if (current >= limit) {
            throw ApiException(
                "QUOTA_EXCEEDED",
                org.springframework.http.HttpStatus.CONFLICT,
                "API-key quota reached for this agent",
                mapOf("limit" to limit, "current" to current, "scope" to "auth.api_keys_per_agent_max"),
            )
        }
    }

    /** `cx_<env>_<base64url(32 random bytes)>` — URL-safe, no padding, ≥256 bits of entropy. */
    private fun generateRawKey(): String {
        val bytes = ByteArray(RANDOM_BYTES)
        rng.nextBytes(bytes)
        val random = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
        return "cx_${props.environment}_$random"
    }

    private fun sha256Hex(raw: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(raw.toByteArray(Charsets.UTF_8))
        return digest.joinToString("") { "%02x".format(it) }
    }

    companion object {
        private val log = LoggerFactory.getLogger(ApiKeyService::class.java)

        /** Random portion length in bytes (256 bits) before base64url expansion. */
        const val RANDOM_BYTES = 32

        /** First-N chars of the raw key persisted/displayed as the prefix (matches Contract 18). */
        const val PREFIX_LEN = 8

        /** Dual-validity window the OLD key stays usable after a rotation (documented default: 24h). */
        val DEFAULT_ROTATION_GRACE: Duration = Duration.ofHours(24)
    }
}
