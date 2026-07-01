package ai.cypherx.auth.service

import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.TenantStatus
import ai.cypherx.auth.repo.PolicyRepository
import ai.cypherx.auth.repo.PolicyRow
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.LoggerFactory
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.stereotype.Service
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.time.Duration
import java.util.UUID

/**
 * Component 4 — the authorization decision engine behind `POST /v1/authorize`.
 *
 * Flow (Phase 2 Component 4 "Decision logic"):
 *  1. Verify the forwarded agent JWT and extract `agent_id`, `tenant_id`, `scopes` ONLY from it
 *     (never from the request body — Contract 13 anti-pattern; the controller rejects a body that
 *     carries agent_id / tenant_id with 400).
 *  2. Load tenant status (platform-scoped `auth.tenants`). If suspended / deleted / pending-deletion
 *     (anything other than active), deny immediately.
 *  3. RBAC: requested `action` MUST be in the token's `scopes` AND allowed by an applicable policy
 *     (per-tenant override > platform default; explicit `deny` wins over `allow`).
 *  4. Write the decision to `auth.audit_log` via the shared [AuditService] (append-only, per-tenant
 *     tamper-evident hash chain — we reuse it so every audit row shares ONE canonical hash format).
 *  5. Return { allowed, reason, policy_ids }.
 *
 * Decisions are cached in Valkey for 30s keyed by
 * `authz:{tenant_id}:{sha256(agent|action|resource|canonical(context))}` (Phase 2 Component 4
 * "Valkey caching"). The cache fails OPEN: any Valkey error is logged at WARN and the decision is
 * computed from the DB — a cache outage must never block authorization platform-wide.
 */
@Service
class AuthorizeService(
    private val jwtMintService: JwtMintService,
    private val policyRepository: PolicyRepository,
    private val auditService: AuditService,
    private val tenantTx: TenantTx,
    private val redis: StringRedisTemplate,
    private val objectMapper: ObjectMapper,
) {

    /**
     * Authorize [action] on [resource] for the agent identified by [forwardedAgentJwt]. [context]
     * is the optional attribute bag from the request body (used in the cache key and reserved for
     * ABAC conditions). [ipAddress] is recorded on the audit row.
     */
    fun authorize(
        forwardedAgentJwt: String,
        action: String,
        resource: String?,
        context: Map<String, Any?>,
        ipAddress: String?,
    ): AuthorizeDecision {
        // 1. Verify the forwarded agent JWT. agent_id + tenant_id come ONLY from here.
        val jwt = jwtMintService.verify(forwardedAgentJwt)
            ?: throw ApiException.unauthorized(
                "X-Forwarded-Agent-JWT is missing, malformed, or failed verification",
                mapOf("header" to "X-Forwarded-Agent-JWT"),
            )
        val claims = jwt.jwtClaimsSet

        val agentId = claims.getStringClaim("agent_id")?.let(::parseUuid)
            ?: claims.subject?.let(::parseUuid)
            ?: throw ApiException.unauthorized("Forwarded JWT has no agent_id/sub")
        val tenantId = claims.getStringClaim("tenant_id")?.let(::parseUuid)
            ?: throw ApiException.unauthorized("Forwarded JWT has no tenant_id")
        val scopes: List<String> = readScopes(claims.getClaim("scopes"))

        // Cache lookup (fail-open). A cache hit short-circuits BEFORE re-evaluating and re-auditing.
        val cacheKey = decisionCacheKey(tenantId, agentId, action, resource, context)
        cachedDecision(cacheKey)?.let { return it }

        // 2. Tenant status (platform-scoped table; deny if not active / unknown).
        val tenantStatus = loadTenantStatus(tenantId)
        val decision = when {
            tenantStatus == null ->
                AuthorizeDecision(false, "Tenant is unknown", emptyList())
            tenantStatus != TenantStatus.ACTIVE ->
                AuthorizeDecision(false, "Tenant is ${tenantStatus.value}", emptyList())
            // 3. RBAC: scope present AND allowed by an applicable policy.
            else -> evaluate(action, resource, scopes, policyRepository.findApplicable(tenantId))
        }

        // 4. Audit (shared chain) + 5. cache the result.
        audit(tenantId, agentId, action, resource, decision, ipAddress)
        putCache(cacheKey, decision)
        return decision
    }

    // ── RBAC evaluation ────────────────────────────────────────────────────────────────────────

    /**
     * Pure decision function. The action must be in the token [scopes] first (Component 4 step 4),
     * then matched against applicable [policies] (step 5). Policies arrive most-specific first
     * (per-tenant before platform default). Within the matched rules, an explicit `deny` wins over
     * `allow`. Every policy that contributes a matching rule is reported in `policy_ids` (by name,
     * matching the seeded `default-allow-first-cycle` identifier in the phase doc examples).
     */
    fun evaluate(
        action: String,
        resource: String?,
        scopes: List<String>,
        policies: List<PolicyRow>,
    ): AuthorizeDecision {
        if (!scopes.contains(action)) {
            return AuthorizeDecision(
                allowed = false,
                reason = "Agent does not have $action scope",
                policyIds = emptyList(),
            )
        }

        val matchingPolicyIds = LinkedHashSet<String>()
        var sawAllow = false
        var sawDeny = false
        for (policy in policies) {
            for (rule in policy.rules) {
                if (rule.action != action) continue
                if (!resourceMatches(rule.resourcePattern, resource)) continue
                matchingPolicyIds += policy.name
                when (rule.effect) {
                    "deny" -> sawDeny = true
                    "allow" -> sawAllow = true
                }
            }
        }

        return when {
            sawDeny -> AuthorizeDecision(
                allowed = false,
                reason = "Action $action denied by policy",
                policyIds = matchingPolicyIds.toList(),
            )
            sawAllow -> AuthorizeDecision(
                allowed = true,
                reason = null,
                policyIds = matchingPolicyIds.toList(),
            )
            else -> AuthorizeDecision(
                allowed = false,
                reason = "No policy permits $action",
                policyIds = emptyList(),
            )
        }
    }

    /** A null/`*` pattern matches any resource; a `prefix:*` glob matches by prefix; else exact match. */
    private fun resourceMatches(pattern: String?, resource: String?): Boolean {
        if (pattern == null || pattern == "*") return true
        if (resource == null) return false
        if (pattern.endsWith(":*")) return resource.startsWith(pattern.dropLast(1))
        return pattern == resource
    }

    // ── Tenant status ────────────────────────────────────────────────────────────────────────

    /** Platform-scoped read of `auth.tenants.status`. Null when the tenant row does not exist. */
    private fun loadTenantStatus(tenantId: UUID): TenantStatus? = tenantTx.inPlatform { jdbc ->
        val status = jdbc.query(
            "SELECT status FROM auth.tenants WHERE tenant_id = ?",
            { rs, _ -> rs.getString("status") },
            tenantId,
        ).firstOrNull()
        status?.let { runCatching { TenantStatus.from(it) }.getOrNull() }
    }

    // ── Audit ────────────────────────────────────────────────────────────────────────────────

    /**
     * Append the decision to `auth.audit_log` through the shared [AuditService], which owns the
     * per-tenant hash chain and pulls request_id/trace_id from the MDC. event_type is
     * `authz.allowed` / `authz.denied`; decision is `allow` / `deny`.
     */
    private fun audit(
        tenantId: UUID,
        agentId: UUID,
        action: String,
        resource: String?,
        decision: AuthorizeDecision,
        ipAddress: String?,
    ) {
        auditService.record(
            eventType = if (decision.allowed) "authz.allowed" else "authz.denied",
            tenantId = tenantId,
            agentId = agentId,
            action = action,
            resource = resource,
            decision = if (decision.allowed) "allow" else "deny",
            policyIds = decision.policyIds,
            ipAddress = ipAddress,
        )
    }

    // ── Valkey decision cache (fail-open) ────────────────────────────────────────────────────

    private fun cachedDecision(cacheKey: String): AuthorizeDecision? =
        try {
            redis.opsForValue().get(cacheKey)?.let { json ->
                objectMapper.readValue(json, AuthorizeDecision::class.java)
            }
        } catch (ex: Exception) {
            log.warn("authz decision cache read failed (failing open to DB): {}", ex.message)
            null
        }

    private fun putCache(cacheKey: String, decision: AuthorizeDecision) {
        try {
            redis.opsForValue().set(cacheKey, objectMapper.writeValueAsString(decision), CACHE_TTL)
        } catch (ex: Exception) {
            log.warn("authz decision cache write failed (ignored): {}", ex.message)
        }
    }

    /**
     * `authz:{tenant_id}:{sha256(agent_id|action|resource|canonical_json(context))}` per
     * Phase 2 Component 4. We canonicalise [context] by serialising a sorted map so equivalent
     * bodies hash identically.
     */
    private fun decisionCacheKey(
        tenantId: UUID,
        agentId: UUID,
        action: String,
        resource: String?,
        context: Map<String, Any?>,
    ): String {
        val canonicalContext = objectMapper.writeValueAsString(context.toSortedMap())
        val material = "$agentId|$action|${resource ?: ""}|$canonicalContext"
        val md = MessageDigest.getInstance("SHA-256")
        val digest = md.digest(material.toByteArray(StandardCharsets.UTF_8))
        return "authz:$tenantId:${hex(digest)}"
    }

    // ── helpers ──────────────────────────────────────────────────────────────────────────────

    private fun parseUuid(value: String): UUID? = runCatching { UUID.fromString(value) }.getOrNull()

    @Suppress("UNCHECKED_CAST")
    private fun readScopes(raw: Any?): List<String> = when (raw) {
        is List<*> -> raw.filterIsInstance<String>()
        is String -> raw.split(" ", ",").map { it.trim() }.filter { it.isNotEmpty() }
        else -> emptyList()
    }

    private fun hex(bytes: ByteArray): String =
        buildString(bytes.size * 2) { bytes.forEach { append("%02x".format(it)) } }

    private companion object {
        val log = LoggerFactory.getLogger(AuthorizeService::class.java)
        val CACHE_TTL: Duration = Duration.ofSeconds(30)
    }
}

/** The decision returned to the caller and serialized as `{ allowed, reason, policy_ids }`. */
data class AuthorizeDecision(
    val allowed: Boolean,
    val reason: String?,
    val policyIds: List<String>,
)
