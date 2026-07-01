package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.AgentStatus
import ai.cypherx.auth.domain.RevocationReason
import ai.cypherx.auth.domain.SYSTEM_USER_ID
import ai.cypherx.auth.domain.TenantStatus
import ai.cypherx.auth.kafka.AuthEventPublisher
import ai.cypherx.auth.repo.AgentRecord
import ai.cypherx.auth.repo.AgentRepository
import ai.cypherx.auth.repo.TenantRepository
import ai.cypherx.auth.web.ApiException
import org.slf4j.LoggerFactory
import org.springframework.dao.DuplicateKeyException
import org.springframework.stereotype.Service
import java.time.Duration
import java.time.Instant
import java.util.UUID

/**
 * Agent registry use cases (Phase 2 Component 1): create an agent and read one back.
 *
 * Authorisation is enforced at the controller (`platform:admin` OR `tenant:admin` for create,
 * presence of a valid agent JWT for read). This service additionally enforces the *tenant-scoping*
 * rule: a `tenant:admin` may only create agents in their OWN tenant; a `platform:admin` may target
 * any tenant. Tenant existence/status is validated against the platform-scoped `auth.tenants` table.
 */
@Service
class AgentService(
    private val agentRepository: AgentRepository,
    private val tenantRepository: TenantRepository,
    private val eventPublisher: AuthEventPublisher,
    private val quotaService: QuotaService,
    private val auditService: AuditService,
    private val apiKeyService: ApiKeyService,
    private val revocationService: RevocationService,
    private val props: AuthProperties,
) {

    /**
     * The verified identity of the caller, lifted from their agent JWT.
     *
     * @param agentId    caller's agent id (`sub` / `agent_id`); used as the `created_by` actor.
     * @param tenantId   caller's tenant (`tenant_id` claim).
     * @param scopes     caller's granted scopes.
     */
    data class Caller(
        val agentId: UUID?,
        val tenantId: UUID?,
        val scopes: Set<String>,
    ) {
        val isPlatformAdmin: Boolean get() = SCOPE_PLATFORM_ADMIN in scopes
        val isTenantAdmin: Boolean get() = SCOPE_TENANT_ADMIN in scopes
    }

    /** Validated create-agent command (shape-checked at the controller). */
    data class CreateAgentCommand(
        val name: String,
        val version: String,
        val allowedScopes: List<String>,
        /** Tenant to create the agent in; null = caller's own tenant. */
        val requestedTenantId: UUID?,
    )

    /**
     * Partial-update command. Each field is OPTIONAL — a null means "leave unchanged"; a present
     * value REPLACES the column. [capabilities]/[metadata] are already-serialized JSON text the
     * controller validated (array / object respectively). At least one field must be present (the
     * controller rejects an all-null patch as 422).
     */
    data class UpdateAgentCommand(
        val allowedScopes: List<String>? = null,
        val capabilitiesJson: String? = null,
        val metadataJson: String? = null,
    )

    /** Result of a deactivate cascade — counts surfaced to the caller / audit trail. */
    data class DeactivationResult(
        val agent: AgentRecord,
        val keysRevoked: Int,
        val tokensRevoked: Int,
        val alreadyInactive: Boolean,
    )

    /**
     * Create an agent. Resolves the target tenant (admin-specified or caller's own), validates the
     * tenant is active, persists the row (`created_by` = caller agent or SYSTEM sentinel), and emits
     * `cypherx.auth.agent.registered`.
     */
    fun createAgent(command: CreateAgentCommand, caller: Caller): AgentRecord {
        val name = command.name.trim()
        if (name.isEmpty()) {
            throw ApiException.validation("Agent name must not be blank", mapOf("field" to "name"))
        }
        val version = command.version.trim().ifEmpty { DEFAULT_VERSION }
        val allowedScopes = command.allowedScopes.map { it.trim() }.filter { it.isNotEmpty() }.distinct()

        val targetTenant = resolveTargetTenant(command.requestedTenantId, caller)
        val tenant = tenantRepository.findById(targetTenant)
            ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to targetTenant.toString()))
        if (tenant.status != TenantStatus.ACTIVE) {
            throw ApiException.conflict(
                "Tenant is not active",
                mapOf("tenant_id" to targetTenant.toString(), "status" to tenant.status.value),
            )
        }

        enforceAgentsMaxQuota(targetTenant)

        val createdBy = caller.agentId ?: SYSTEM_USER_ID

        val agent = try {
            agentRepository.insert(
                tenantId = targetTenant,
                name = name,
                version = version,
                allowedScopes = allowedScopes,
                capabilities = "[]",
                metadata = "{}",
                createdBy = createdBy,
            )
        } catch (ex: DuplicateKeyException) {
            throw ApiException.conflict(
                "An agent with this name and version already exists in the tenant",
                mapOf("name" to name, "version" to version, "tenant_id" to targetTenant.toString()),
            )
        }

        // Best-effort lifecycle event (compact topic, keyed by agent_id). Never blocks the response.
        eventPublisher.agentRegistered(
            agentId = agent.agentId,
            tenantId = agent.tenantId,
            plan = tenant.plan,
            createdAt = agent.createdAt,
        )

        // Durable audit (Component 6 — issuance-event coverage). Best-effort; never blocks the create.
        runCatching {
            auditService.record(
                eventType = "agent.registered",
                tenantId = agent.tenantId,
                agentId = caller.agentId,
                action = "agent:create",
                resource = "agent:${agent.agentId}",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for agent.registered {}: {}", agent.agentId, it.message) }

        log.info("registered agent {} (name={}) in tenant {}", agent.agentId, name, targetTenant)
        return agent
    }

    /**
     * Read an agent by id. A `platform:admin` may read any tenant's agent (when they pass the agent's
     * tenant); otherwise the lookup is scoped to the caller's own tenant. Returns 404 when absent or
     * not visible under RLS.
     */
    fun getAgent(agentId: UUID, caller: Caller): AgentRecord {
        val tenantId = caller.tenantId
            ?: throw ApiException.unauthorized("Caller tenant could not be resolved")
        return agentRepository.findById(tenantId, agentId)
            ?: throw ApiException.notFound("Agent not found", mapOf("agent_id" to agentId.toString()))
    }

    /**
     * Keyset-paginated list of the caller tenant's agents (RLS-scoped). [statusFilter] is validated
     * against [AgentStatus]; [nameContains] is a case-insensitive substring. [cursor] is the opaque
     * `<epochMillis>_<agentId>` token returned as `next_cursor` on the previous page. [limit] is
     * clamped 1..[MAX_PAGE_SIZE]. Returns the page plus the next cursor (null when the page is the
     * last). One extra row is fetched to decide whether a further page exists without a count query.
     */
    fun listAgents(
        caller: Caller,
        statusFilter: String?,
        nameContains: String?,
        cursor: String?,
        limit: Int,
    ): AgentPage {
        val tenantId = caller.tenantId
            ?: throw ApiException.unauthorized("Caller tenant could not be resolved")

        val status = statusFilter?.takeIf { it.isNotBlank() }?.let {
            runCatching { AgentStatus.from(it.trim()) }.getOrElse {
                throw ApiException.validation(
                    "Unknown status filter",
                    mapOf("field" to "status", "allowed" to AgentStatus.entries.map { s -> s.value }),
                )
            }.value
        }
        val name = nameContains?.trim()?.takeIf { it.isNotEmpty() }
        val capped = limit.coerceIn(1, MAX_PAGE_SIZE)
        val (afterCreatedAt, afterAgentId) = decodeCursor(cursor)

        // Fetch capped+1 to detect a following page (the extra row is dropped from the response).
        val rows = agentRepository.list(
            tenantId = tenantId,
            statusFilter = status,
            nameContains = name,
            afterCreatedAt = afterCreatedAt,
            afterAgentId = afterAgentId,
            limit = capped + 1,
        )
        val hasMore = rows.size > capped
        val page = if (hasMore) rows.subList(0, capped) else rows
        val nextCursor = if (hasMore) page.lastOrNull()?.let { encodeCursor(it.createdAt, it.agentId) } else null
        return AgentPage(agents = page, nextCursor = nextCursor)
    }

    /**
     * Partially update [agentId] in the caller's tenant. Only the present command fields change
     * (allowed_scopes / capabilities / metadata); the row's `updated_at` is bumped. Audits
     * `agent.updated` and emits the advisory `cypherx.auth.agent.updated` event (WP02 wiring) so
     * /authorize caches invalidate. 404 when the agent is absent / RLS-invisible.
     */
    fun updateAgent(agentId: UUID, command: UpdateAgentCommand, caller: Caller): AgentRecord {
        val tenantId = caller.tenantId
            ?: throw ApiException.unauthorized("Caller tenant could not be resolved")

        val allowedScopes = command.allowedScopes
            ?.map { it.trim() }?.filter { it.isNotEmpty() }?.distinct()

        // Pre-flight existence check so an absent agent is a clean 404 (RETURNING would also yield
        // empty, but this keeps the not-found path explicit and symmetrical with getAgent).
        agentRepository.findById(tenantId, agentId)
            ?: throw ApiException.notFound("Agent not found", mapOf("agent_id" to agentId.toString()))

        val updated = agentRepository.updatePartial(
            tenantId = tenantId,
            agentId = agentId,
            allowedScopes = allowedScopes,
            capabilities = command.capabilitiesJson,
            metadata = command.metadataJson,
        ) ?: throw ApiException.notFound("Agent not found", mapOf("agent_id" to agentId.toString()))

        runCatching {
            auditService.record(
                eventType = "agent.updated",
                tenantId = tenantId,
                agentId = caller.agentId,
                action = "agent:update",
                resource = "agent:$agentId",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for agent.updated {}: {}", agentId, it.message) }

        // Advisory event — drives /authorize agent-cap cache invalidation (best-effort).
        runCatching {
            eventPublisher.agentUpdated(
                agentId = updated.agentId,
                tenantId = updated.tenantId,
                status = updated.status,
                updatedAt = updated.updatedAt,
            )
        }.onFailure { log.warn("agent.updated publish failed for agent {}: {}", agentId, it.message) }

        log.info("updated agent {} in tenant {}", agentId, tenantId)
        return updated
    }

    /**
     * Deactivate [agentId] and CASCADE: revoke all its API keys, revoke ALL its live tokens (via
     * [RevocationService.revokeAllForAgent], which also suspends the agent + emits agent.updated),
     * then stamp the agent's status to `inactive`. Audited `agent.deactivated`. 404 when absent.
     *
     * Order matters: keys first (no NEW token can be minted from a revoked key), then live-token
     * revoke-all (kills tokens already in flight and blocks re-mint by suspending), then the final
     * `inactive` status. Idempotent — re-deactivating an already-inactive agent re-runs the (no-op)
     * cascade and reports `alreadyInactive = true`.
     */
    fun deactivateAgent(agentId: UUID, caller: Caller): DeactivationResult {
        val tenantId = caller.tenantId
            ?: throw ApiException.unauthorized("Caller tenant could not be resolved")
        val existing = agentRepository.findById(tenantId, agentId)
            ?: throw ApiException.notFound("Agent not found", mapOf("agent_id" to agentId.toString()))
        val alreadyInactive = existing.status == AgentStatus.INACTIVE.value

        val revokedBy = caller.agentId ?: SYSTEM_USER_ID

        // 1. Revoke every API key for the agent (cascade — blocks new token exchanges).
        val keysRevoked = apiKeyService.revokeAllForAgent(tenantId, agentId, revokedBy)

        // 2. Revoke ALL live tokens + suspend (existing WP03 service — do NOT re-implement). This
        //    sets status=suspended and emits agent.updated; we override to `inactive` in step 3.
        val tokensRevoked = revocationService.revokeAllForAgent(
            agentId = agentId,
            tenantId = tenantId,
            reason = RevocationReason.DEACTIVATED,
            revokedBy = revokedBy,
            defaultTokenTtl = Duration.ofSeconds(props.agentTokenTtlSeconds),
        )

        // 3. Final desired state: inactive (deactivation, not a temporary suspend).
        val deactivated = agentRepository.updateStatus(tenantId, agentId, AgentStatus.INACTIVE.value)
            ?: existing

        runCatching {
            auditService.record(
                eventType = "agent.deactivated",
                tenantId = tenantId,
                agentId = caller.agentId,
                action = "agent:deactivate",
                resource = "agent:$agentId",
                decision = "allow",
            )
        }.onFailure { log.warn("audit write failed for agent.deactivated {}: {}", agentId, it.message) }

        // Emit the final inactive state (RevocationService already emitted `suspended`; this reflects
        // the terminal status so caches converge on `inactive`). Best-effort.
        runCatching {
            eventPublisher.agentUpdated(
                agentId = deactivated.agentId,
                tenantId = deactivated.tenantId,
                status = deactivated.status,
                updatedAt = deactivated.updatedAt,
            )
        }.onFailure { log.warn("agent.updated (deactivate) publish failed for agent {}: {}", agentId, it.message) }

        log.info(
            "deactivated agent {} in tenant {} (keys_revoked={}, tokens_revoked={})",
            agentId, tenantId, keysRevoked, tokensRevoked,
        )
        return DeactivationResult(
            agent = deactivated,
            keysRevoked = keysRevoked,
            tokensRevoked = tokensRevoked,
            alreadyInactive = alreadyInactive,
        )
    }

    /** A page of agents plus the opaque cursor for the next page (null at end of list). */
    data class AgentPage(val agents: List<AgentRecord>, val nextCursor: String?)

    /**
     * Resolve which tenant the new agent belongs to:
     *  - no requested tenant → caller's own tenant (must be present);
     *  - requested == caller's own → allowed for tenant:admin and platform:admin;
     *  - requested != caller's own → platform:admin ONLY.
     */
    /**
     * Enforce the Contract-19 `auth.agents_max` quota for [tenantId] (resource cap, not a rate). A
     * limit <= 0 / absent means unlimited. Quota RESOLUTION fails open (a quota-subsystem glitch must
     * not block agent creation); only a real at-or-over-limit count rejects with 409 QUOTA_EXCEEDED.
     */
    private fun enforceAgentsMaxQuota(tenantId: UUID) {
        val limit = runCatching {
            quotaService.effectiveLimits(tenantId).path("auth").path("agents_max").asInt(0)
        }.getOrElse {
            log.debug("agents_max quota resolve skipped for tenant {}: {}", tenantId, it.message)
            return // fail-open
        }
        if (limit <= 0) return // unlimited / not configured
        val current = agentRepository.countByTenant(tenantId)
        if (current >= limit) {
            throw ApiException(
                "QUOTA_EXCEEDED",
                org.springframework.http.HttpStatus.CONFLICT,
                "Agent quota reached for this tenant",
                mapOf("limit" to limit, "current" to current, "scope" to "auth.agents_max"),
            )
        }
    }

    /**
     * Encode a keyset cursor as `<createdAtEpochMillis>_<agentId>` — the stable composite ordering
     * key the next page resumes after. Opaque to callers (round-tripped verbatim).
     */
    private fun encodeCursor(createdAt: Instant, agentId: UUID): String =
        "${createdAt.toEpochMilli()}_$agentId"

    /**
     * Decode a `<epochMillis>_<agentId>` cursor into its (createdAt, agentId) parts. A blank/absent
     * cursor → (null, null) = first page. A malformed cursor is a 422 (callers must round-trip the
     * exact `next_cursor` we issued).
     */
    private fun decodeCursor(cursor: String?): Pair<Instant?, UUID?> {
        val raw = cursor?.takeIf { it.isNotBlank() } ?: return null to null
        val sep = raw.lastIndexOf('_')
        val parsed = if (sep > 0) {
            val millis = raw.substring(0, sep).toLongOrNull()
            val id = runCatching { UUID.fromString(raw.substring(sep + 1)) }.getOrNull()
            if (millis != null && id != null) Instant.ofEpochMilli(millis) to id else null
        } else {
            null
        }
        return parsed ?: throw ApiException.validation("Invalid cursor", mapOf("field" to "cursor"))
    }

    private fun resolveTargetTenant(requested: UUID?, caller: Caller): UUID {
        val callerTenant = caller.tenantId
        if (requested == null) {
            return callerTenant
                ?: throw ApiException.validation(
                    "tenant_id is required (caller token carries no tenant)",
                    mapOf("field" to "tenant_id"),
                )
        }
        if (requested != callerTenant && !caller.isPlatformAdmin) {
            throw ApiException.forbidden(
                "Only platform:admin may create agents in another tenant",
                mapOf("requested_tenant_id" to requested.toString()),
            )
        }
        return requested
    }

    private companion object {
        val log = LoggerFactory.getLogger(AgentService::class.java)
        const val DEFAULT_VERSION = "1.0.0"
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
        const val SCOPE_TENANT_ADMIN = "tenant:admin"

        /** Max agents returned per LIST page (documented default; the controller default is smaller). */
        const val MAX_PAGE_SIZE = 200
    }
}
