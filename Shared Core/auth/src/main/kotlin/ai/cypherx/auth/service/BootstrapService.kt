package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.PLATFORM_TENANT_ID
import ai.cypherx.auth.domain.SYSTEM_USER_ID
import ai.cypherx.auth.kafka.AuthEventPublisher
import ai.cypherx.auth.repo.AgentRepository
import ai.cypherx.auth.web.ApiException
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.time.Instant
import java.util.UUID

/**
 * One-time super-admin bootstrap (Phase 2 Component 1, "Bootstrap super-admin path").
 *
 * The very first agent has no caller JWT, so it cannot be created through the scope-gated
 * `POST /v1/agents`. Instead `POST /v1/admin/bootstrap` accepts ONE request bearing
 * `X-Bootstrap-Token: <AuthProperties.bootstrapToken>` and:
 *   1. verifies the header equals the configured bootstrap token (constant-time compare),
 *   2. only when bootstrap has not already completed (no `auth.bootstrap_state` sentinel row),
 *   3. creates the first agent with `platform:admin` scope in the PLATFORM tenant
 *      (`created_by` = SYSTEM-USER sentinel — there is no px0 user behind a bootstrap agent), and
 *   4. inserts the `auth.bootstrap_state` sentinel so the token is permanently rejected (410 Gone)
 *      thereafter. All subsequent admin ops use normal JWT auth with `platform:admin` scope.
 *
 * `auth.bootstrap_state` is platform-scoped (no RLS) — read/written via [TenantTx.inPlatform]. The
 * sentinel row IS the authoritative "bootstrap complete" marker (RLS makes a platform-wide agent
 * count impossible without bypassing RLS, so the sentinel is the correct gate).
 */
@Service
class BootstrapService(
    private val tenantTx: TenantTx,
    private val agentRepository: AgentRepository,
    private val apiKeyService: ApiKeyService,
    private val eventPublisher: AuthEventPublisher,
    private val props: AuthProperties,
) {

    /** Result returned to the controller: the super-admin agent + its initial API key (shown ONCE). */
    data class BootstrapResult(
        val agentId: UUID,
        val tenantId: UUID,
        val name: String,
        val allowedScopes: List<String>,
        val createdAt: Instant,
        val apiKeyId: UUID,
        val apiKey: String,
        val keyPrefix: String,
    )

    /**
     * Execute the one-time bootstrap.
     *
     * @param presentedToken value of the `X-Bootstrap-Token` request header (may be null/blank).
     * @param name           name for the first super-admin agent (defaults to "bootstrap-admin").
     * @throws ApiException 503 if no bootstrap token is configured, 401 if the header is missing or
     *         does not match, 410 Gone if bootstrap already completed.
     */
    fun bootstrap(presentedToken: String?, name: String?): BootstrapResult {
        val configured = props.bootstrapToken?.takeIf { it.isNotBlank() }
            ?: throw ApiException.serviceUnavailable(
                "Bootstrap is not enabled on this deployment (no bootstrap token configured)",
            )

        if (presentedToken.isNullOrBlank() || !constantTimeEquals(presentedToken, configured)) {
            throw ApiException.unauthorized(
                "Invalid or missing X-Bootstrap-Token",
                mapOf("header" to "X-Bootstrap-Token"),
            )
        }

        // Gate: if the sentinel already exists, bootstrap is permanently closed (410 Gone).
        if (isBootstrapped()) {
            throw ApiException.gone(
                "Bootstrap has already completed; use a platform:admin JWT for admin operations",
            )
        }

        val agentName = name?.trim()?.takeIf { it.isNotEmpty() } ?: DEFAULT_ADMIN_NAME

        // Create the first super-admin agent in the platform tenant.
        val agent = try {
            agentRepository.insert(
                tenantId = PLATFORM_TENANT_ID,
                name = agentName,
                version = DEFAULT_VERSION,
                allowedScopes = listOf(SCOPE_PLATFORM_ADMIN),
                capabilities = "[]",
                metadata = """{"bootstrap":true}""",
                createdBy = SYSTEM_USER_ID,
            )
        } catch (ex: org.springframework.dao.DuplicateKeyException) {
            // A super-admin by this name already exists in the platform tenant.
            throw ApiException.conflict(
                "A platform agent named '$agentName' already exists",
                mapOf("name" to agentName, "version" to DEFAULT_VERSION),
            )
        }

        // Flip the platform-scoped bootstrap_state sentinel (single-row table; id is always TRUE).
        markBootstrapped(agent.createdBy)

        // Best-effort lifecycle event (compact topic, keyed by agent_id). Never blocks the response.
        eventPublisher.agentRegistered(
            agentId = agent.agentId,
            tenantId = agent.tenantId,
            plan = PLATFORM_PLAN,
            createdAt = agent.createdAt,
        )

        // Issue the super-admin's initial API key (returned ONCE). Without this the bootstrap admin
        // would have no credential to mint a JWT, so the entire authenticated admin surface
        // (create tenants/agents/keys) would be unreachable — a bootstrap chicken-and-egg.
        val adminKey = apiKeyService.issue(
            tenantId = agent.tenantId,
            agentId = agent.agentId,
            scopes = listOf(SCOPE_PLATFORM_ADMIN),
            name = "bootstrap-admin-key",
            expiresInDays = null,
        )

        log.info("bootstrap complete — created super-admin agent {} + initial key in platform tenant", agent.agentId)

        return BootstrapResult(
            agentId = agent.agentId,
            tenantId = agent.tenantId,
            name = agent.name,
            allowedScopes = agent.allowedScopes,
            createdAt = agent.createdAt,
            apiKeyId = adminKey.keyId,
            apiKey = adminKey.rawKey,
            keyPrefix = adminKey.keyPrefix,
        )
    }

    /** True once the one-time bootstrap sentinel row exists. */
    fun isBootstrapped(): Boolean = tenantTx.inPlatform { jdbc ->
        jdbc.queryForObject("SELECT EXISTS (SELECT 1 FROM auth.bootstrap_state)", Boolean::class.java) ?: false
    }

    private fun markBootstrapped(completedBy: UUID) {
        tenantTx.inPlatform { jdbc ->
            jdbc.update(
                """
                INSERT INTO auth.bootstrap_state (id, completed_at, completed_by)
                VALUES (TRUE, NOW(), ?)
                ON CONFLICT (id) DO NOTHING
                """.trimIndent(),
                completedBy,
            )
        }
    }

    /** Length-constant comparison so a near-miss token cannot be timing-distinguished. */
    private fun constantTimeEquals(a: String, b: String): Boolean =
        java.security.MessageDigest.isEqual(a.toByteArray(Charsets.UTF_8), b.toByteArray(Charsets.UTF_8))

    private companion object {
        val log = LoggerFactory.getLogger(BootstrapService::class.java)
        const val DEFAULT_ADMIN_NAME = "bootstrap-admin"
        const val DEFAULT_VERSION = "1.0.0"
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
        const val PLATFORM_PLAN = "enterprise"
    }
}
