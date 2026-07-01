package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.AgentType
import ai.cypherx.auth.repo.AgentRepository
import ai.cypherx.auth.repo.ApprovalRequestRow
import ai.cypherx.auth.repo.HilConfigRow
import ai.cypherx.auth.repo.HilRepository
import ai.cypherx.auth.web.ApiException
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.time.Duration
import java.time.Instant
import java.util.UUID

/**
 * Human-in-the-Loop approval workflow (Phase 6).
 *
 * An agent (typically a sub-agent) about to perform an ``ask``-mode action asks
 * [requestApproval]. The decision keys off the controlling ORCHESTRATOR's HIL mode:
 *   * automated     -> auto-approved (no human, no pending row).
 *   * human_in_loop -> a pending request is created; the user must grant/deny it.
 *   * partial       -> pending only if the operation_type is in ``ask_on_triggers``; else auto.
 *
 * The controlling orchestrator is the caller itself when it IS the orchestrator, else its
 * ``parent_orchestrator_id``. A ``user_created`` agent (no orchestrator) defaults to automated.
 */
@Service
class HilService(
    private val hilRepository: HilRepository,
    private val agentRepository: AgentRepository,
    private val props: AuthProperties,
) {

    /** Outcome of an approval request: auto-approved (proceed now) or a pending request to poll. */
    data class AskResult(
        val autoApproved: Boolean,
        val requestId: UUID?,
        val status: String, // granted | pending
    )

    fun requestApproval(
        tenantId: UUID,
        agentId: UUID,
        operationType: String,
        operationContextJson: String,
    ): AskResult {
        val orchestratorId = resolveOrchestratorId(tenantId, agentId)
        val mode = orchestratorId
            ?.let { hilRepository.getHilConfig(tenantId, it) }
            ?: HilConfigRow(orchestratorId ?: agentId, DEFAULT_MODE, emptyList())

        val mustAsk = when (mode.defaultMode) {
            MODE_HUMAN_IN_LOOP -> true
            MODE_PARTIAL -> operationType in mode.askOnTriggers
            else -> false // automated
        }
        if (!mustAsk) {
            return AskResult(autoApproved = true, requestId = null, status = "granted")
        }
        val expiresAt = Instant.now().plus(Duration.ofSeconds(props.hilApprovalTtlSeconds))
        val row = hilRepository.insertRequest(tenantId, agentId, operationType, operationContextJson, expiresAt)
        log.info("hil_request_created request={} tenant={} op={}", row.requestId, tenantId, operationType)
        return AskResult(autoApproved = false, requestId = row.requestId, status = "pending")
    }

    /** Current status of a request, lazily flipping an elapsed pending row to `expired`. */
    fun getStatus(tenantId: UUID, requestId: UUID): ApprovalRequestRow {
        val row = hilRepository.getRequest(tenantId, requestId)
            ?: throw ApiException.notFound("Approval request not found", mapOf("request_id" to requestId.toString()))
        if (row.status == "pending" && Instant.now().isAfter(row.expiresAt)) {
            hilRepository.resolve(tenantId, requestId, "expired", row.agentId, "auto-expired")
            return row.copy(status = "expired")
        }
        return row
    }

    fun listPending(tenantId: UUID, operationType: String?): List<ApprovalRequestRow> =
        hilRepository.listPending(tenantId, operationType)

    fun grant(tenantId: UUID, requestId: UUID, resolvedBy: UUID, note: String?): ApprovalRequestRow =
        resolveRequest(tenantId, requestId, "granted", resolvedBy, note)

    fun deny(tenantId: UUID, requestId: UUID, resolvedBy: UUID, note: String?): ApprovalRequestRow =
        resolveRequest(tenantId, requestId, "denied", resolvedBy, note)

    fun getConfig(tenantId: UUID, orchestratorId: UUID): HilConfigRow =
        hilRepository.getHilConfig(tenantId, orchestratorId)
            ?: HilConfigRow(orchestratorId, DEFAULT_MODE, emptyList())

    fun setConfig(
        tenantId: UUID,
        orchestratorId: UUID,
        mode: String,
        triggers: List<String>,
    ): HilConfigRow {
        if (mode !in VALID_MODES) {
            throw ApiException.validation(
                "default_mode must be one of $VALID_MODES",
                mapOf("field" to "default_mode"),
            )
        }
        // Only an orchestrator's config is meaningful.
        val agent = agentRepository.findById(tenantId, orchestratorId)
            ?: throw ApiException.notFound("Agent not found", mapOf("agent_id" to orchestratorId.toString()))
        if (agent.agentType != AgentType.ORCHESTRATOR.value) {
            throw ApiException.validation("HIL config applies to the orchestrator agent only")
        }
        return hilRepository.upsertHilConfig(tenantId, orchestratorId, mode, triggers.map { it.trim() }.filter { it.isNotEmpty() })
    }

    private fun resolveRequest(
        tenantId: UUID,
        requestId: UUID,
        decision: String,
        resolvedBy: UUID,
        note: String?,
    ): ApprovalRequestRow {
        val existing = hilRepository.getRequest(tenantId, requestId)
            ?: throw ApiException.notFound("Approval request not found", mapOf("request_id" to requestId.toString()))
        if (existing.status != "pending") {
            throw ApiException.conflict(
                "Approval request is already ${existing.status}",
                mapOf("request_id" to requestId.toString(), "status" to existing.status),
            )
        }
        if (!hilRepository.resolve(tenantId, requestId, decision, resolvedBy, note)) {
            throw ApiException.conflict("Approval request could not be resolved (already resolved?)")
        }
        log.info("hil_request_resolved request={} decision={} by={}", requestId, decision, resolvedBy)
        return existing.copy(status = decision, resolvedAt = Instant.now(), resolutionNote = note)
    }

    /** The controlling orchestrator for [agentId]: itself if orchestrator, else its parent. */
    private fun resolveOrchestratorId(tenantId: UUID, agentId: UUID): UUID? {
        val agent = agentRepository.findById(tenantId, agentId) ?: return null
        return when (agent.agentType) {
            AgentType.ORCHESTRATOR.value -> agent.agentId
            AgentType.SUB_AGENT.value -> agent.parentOrchestratorId
            else -> null
        }
    }

    private companion object {
        val log = LoggerFactory.getLogger(HilService::class.java)
        const val MODE_HUMAN_IN_LOOP = "human_in_loop"
        const val MODE_PARTIAL = "partial"
        const val DEFAULT_MODE = "automated"
        val VALID_MODES = setOf("automated", "human_in_loop", "partial")
    }
}
