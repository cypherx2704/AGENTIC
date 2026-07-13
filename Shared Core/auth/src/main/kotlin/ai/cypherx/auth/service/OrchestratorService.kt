package ai.cypherx.auth.service

import ai.cypherx.auth.domain.AgentType
import ai.cypherx.auth.repo.AgentRecord
import ai.cypherx.auth.repo.AgentRepository
import ai.cypherx.auth.web.ApiException
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.util.UUID

/**
 * Orchestrator hierarchy use cases (orchestrator-only sub-agent management).
 *
 * The orchestrator is the single mandatory agent auto-created per tenant. It is the ONLY agent that
 * may create sub-agents, and it may only manage the sub-agents IT created — never `user_created`
 * agents and never itself. Invariants enforced here:
 *
 *  - Caller MUST be an `orchestrator` (verified against the DB agent_type, not just the JWT scope).
 *  - A `sub_agent` calling create → 403 SUB_AGENT_CANNOT_DELEGATE (depth = 1).
 *  - Sub-agent scopes MUST be a subset of the orchestrator's `allowed_scopes` (422 otherwise).
 *  - PATCH/DELETE target MUST have `parent_orchestrator_id == caller` (else 404 — invisible to it).
 *
 * Agent creation/lifecycle is delegated to [AgentService] (quota, audit, events) — this service
 * adds only the hierarchy rules on top.
 */
@Service
class OrchestratorService(
    private val agentRepository: AgentRepository,
    private val agentService: AgentService,
    private val tokenMintService: TokenMintService,
) {

    /** Resolve + assert the caller is this tenant's orchestrator; returns its record. */
    private fun requireOrchestrator(caller: AgentService.Caller): AgentRecord {
        val tenantId = caller.tenantId
            ?: throw ApiException.unauthorized("Caller tenant could not be resolved")
        val agentId = caller.agentId
            ?: throw ApiException.forbidden("Caller is not an agent")
        val me = agentRepository.findById(tenantId, agentId)
            ?: throw ApiException.forbidden("Caller agent not found")
        when (me.agentType) {
            AgentType.ORCHESTRATOR.value -> return me
            AgentType.SUB_AGENT.value -> throw ApiException(
                "SUB_AGENT_CANNOT_DELEGATE",
                org.springframework.http.HttpStatus.FORBIDDEN,
                "A sub-agent cannot create or manage sub-agents (depth is limited to 1)",
            )
            else -> throw ApiException.forbidden("Only the tenant orchestrator may manage sub-agents")
        }
    }

    /**
     * Create a sub-agent owned by the calling orchestrator. The requested scopes must be a subset of
     * the orchestrator's own `allowed_scopes`. The new agent is `sub_agent` with
     * `parent_orchestrator_id = orchestrator.agent_id` and inherits the orchestrator's owner_user_id.
     */
    fun createSubAgent(
        caller: AgentService.Caller,
        name: String,
        version: String,
        requestedScopes: List<String>,
    ): AgentRecord {
        val orchestrator = requireOrchestrator(caller)
        val cleanScopes = requestedScopes.map { it.trim() }.filter { it.isNotEmpty() }.distinct()

        val orchestratorScopes = orchestrator.allowedScopes.toSet()
        val exceeding = cleanScopes.filter { it !in orchestratorScopes }
        if (exceeding.isNotEmpty()) {
            throw ApiException.validation(
                "Sub-agent scopes must be a subset of the orchestrator's allowed_scopes",
                mapOf("exceeding" to exceeding, "orchestrator_scopes" to orchestrator.allowedScopes),
            )
        }

        return agentService.createAgent(
            AgentService.CreateAgentCommand(
                name = name,
                version = version,
                allowedScopes = cleanScopes,
                requestedTenantId = orchestrator.tenantId,
                agentType = AgentType.SUB_AGENT,
                parentOrchestratorId = orchestrator.agentId,
                ownerUserId = orchestrator.ownerUserId,
            ),
            caller,
        )
    }

    /** List the sub-agents owned by the calling orchestrator (keyset-paginated, newest-first). */
    fun listSubAgents(caller: AgentService.Caller, cursor: String?, limit: Int): AgentService.AgentPage {
        val orchestrator = requireOrchestrator(caller)
        val capped = limit.coerceIn(1, 200)
        val (afterCreatedAt, afterAgentId) = decodeCursor(cursor)
        val rows = agentRepository.list(
            tenantId = orchestrator.tenantId,
            statusFilter = null,
            nameContains = null,
            afterCreatedAt = afterCreatedAt,
            afterAgentId = afterAgentId,
            limit = capped + 1,
            parentOrchestratorId = orchestrator.agentId,
        )
        val hasMore = rows.size > capped
        val page = if (hasMore) rows.subList(0, capped) else rows
        val next = if (hasMore) page.lastOrNull()?.let { "${it.createdAt.toEpochMilli()}_${it.agentId}" } else null
        return AgentService.AgentPage(agents = page, nextCursor = next)
    }

    /** Decode a `<epochMillis>_<agentId>` cursor into (createdAt, agentId); blank → first page. */
    private fun decodeCursor(cursor: String?): Pair<java.time.Instant?, UUID?> {
        val raw = cursor?.takeIf { it.isNotBlank() } ?: return null to null
        val sep = raw.lastIndexOf('_')
        if (sep <= 0) throw ApiException.validation("Invalid cursor", mapOf("field" to "cursor"))
        val millis = raw.substring(0, sep).toLongOrNull()
        val id = runCatching { UUID.fromString(raw.substring(sep + 1)) }.getOrNull()
        if (millis == null || id == null) throw ApiException.validation("Invalid cursor", mapOf("field" to "cursor"))
        return java.time.Instant.ofEpochMilli(millis) to id
    }

    /**
     * Partially update a sub-agent owned by the calling orchestrator. `allowed_scopes` (when present)
     * must still be a subset of the orchestrator's scopes. 404 when the target is not a sub-agent of
     * this orchestrator (RBAC boundary — the orchestrator cannot even see others' agents here).
     */
    fun updateSubAgent(
        caller: AgentService.Caller,
        subAgentId: UUID,
        allowedScopes: List<String>?,
        capabilitiesJson: String?,
        metadataJson: String?,
    ): AgentRecord {
        val orchestrator = requireOrchestrator(caller)
        val target = agentRepository.findById(orchestrator.tenantId, subAgentId)
            ?: throw ApiException.notFound("Sub-agent not found", mapOf("agent_id" to subAgentId.toString()))
        if (target.parentOrchestratorId != orchestrator.agentId) {
            throw ApiException.notFound(
                "Sub-agent not found for this orchestrator",
                mapOf("agent_id" to subAgentId.toString()),
            )
        }

        val cleanScopes = allowedScopes?.map { it.trim() }?.filter { it.isNotEmpty() }?.distinct()
        if (cleanScopes != null) {
            val orchestratorScopes = orchestrator.allowedScopes.toSet()
            val exceeding = cleanScopes.filter { it !in orchestratorScopes }
            if (exceeding.isNotEmpty()) {
                throw ApiException.validation(
                    "Sub-agent scopes must be a subset of the orchestrator's allowed_scopes",
                    mapOf("exceeding" to exceeding),
                )
            }
        }

        return agentService.updateAgent(
            subAgentId,
            AgentService.UpdateAgentCommand(
                allowedScopes = cleanScopes,
                capabilitiesJson = capabilitiesJson,
                metadataJson = metadataJson,
            ),
            caller,
        )
    }

    /**
     * Mint a short-lived agent JWT FOR one of the calling orchestrator's own sub-agents (delegation
     * mint — no api_key required). This is how the orchestration engine gives a sub-agent task its
     * own identity so downstream services (LLMs gateway alias allowlist, Tools registry access)
     * enforce the SUB-AGENT's confinement, not the orchestrator's.
     *
     * Authz (this method): caller MUST be the tenant orchestrator; the target MUST be a `sub_agent`
     * with `parent_orchestrator_id == caller` (else 404 — invisible). The requested scopes are
     * intersected with the sub-agent's own `allowed_scopes` by [TokenMintService.mintForAgent] (which
     * are already a subset of the orchestrator's by the creation-time invariant), so the minted token
     * can never exceed the sub-agent's persisted authority.
     */
    fun mintSubAgentToken(
        caller: AgentService.Caller,
        subAgentId: UUID,
        requestedScopes: List<String>,
    ): TokenMintService.MintedAccessToken {
        val orchestrator = requireOrchestrator(caller)
        val target = agentRepository.findById(orchestrator.tenantId, subAgentId)
        // Single, IDENTICAL 404 for every not-mintable case (absent / not-a-sub-agent / owned by
        // another orchestrator) so the response can't distinguish existence from ownership.
        if (target == null ||
            target.agentType != AgentType.SUB_AGENT.value ||
            target.parentOrchestratorId != orchestrator.agentId
        ) {
            throw ApiException.notFound("Sub-agent not found", mapOf("agent_id" to subAgentId.toString()))
        }
        return tokenMintService.mintForAgent(orchestrator.tenantId, subAgentId, requestedScopes)
    }

    /** Deactivate a sub-agent owned by the calling orchestrator (cascade revoke via AgentService). */
    fun deactivateSubAgent(caller: AgentService.Caller, subAgentId: UUID): AgentService.DeactivationResult {
        val orchestrator = requireOrchestrator(caller)
        val target = agentRepository.findById(orchestrator.tenantId, subAgentId)
            ?: throw ApiException.notFound("Sub-agent not found", mapOf("agent_id" to subAgentId.toString()))
        if (target.parentOrchestratorId != orchestrator.agentId) {
            throw ApiException.notFound(
                "Sub-agent not found for this orchestrator",
                mapOf("agent_id" to subAgentId.toString()),
            )
        }
        return agentService.deactivateAgent(subAgentId, caller)
    }

    private companion object {
        val log = LoggerFactory.getLogger(OrchestratorService::class.java)
    }
}
