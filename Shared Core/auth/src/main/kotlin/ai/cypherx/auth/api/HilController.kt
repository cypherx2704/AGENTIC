package ai.cypherx.auth.api

import ai.cypherx.auth.repo.ApprovalRequestRow
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.HilService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.annotation.JsonInclude
import com.fasterxml.jackson.annotation.JsonProperty
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.PutMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import java.time.Instant
import java.util.UUID

/**
 * Human-in-the-Loop approval surface (Phase 6).
 *
 * Agent side (the requester — any authenticated agent; identity from JWT):
 *  - `POST /v1/hil/approvals/request`   create/decide a request -> { auto_approved, request_id, status }
 *  - `GET  /v1/hil/approvals/{id}`      poll a request's status
 *
 * Human side (the orchestrator/console — scope `hil:approve`):
 *  - `GET  /v1/hil/approvals`           list pending requests
 *  - `POST /v1/hil/approvals/{id}/grant`
 *  - `POST /v1/hil/approvals/{id}/deny`
 *
 * Orchestrator HIL mode config (scope `orchestrator:manage` / `tenant:admin`):
 *  - `GET  /v1/orchestrator/hil-config`
 *  - `PUT  /v1/orchestrator/hil-config`
 */
@RestController
class HilController(
    private val hilService: HilService,
    private val callerContext: CallerContext,
    private val objectMapper: ObjectMapper,
) {

    data class RequestApprovalBody(
        @JsonProperty("operation_type") val operationType: String? = null,
        val context: JsonNode? = null,
    )

    data class AskResponse(
        @JsonProperty("auto_approved") val autoApproved: Boolean,
        @JsonProperty("request_id") val requestId: UUID?,
        val status: String,
    )

    data class ApprovalView(
        @JsonProperty("request_id") val requestId: UUID,
        @JsonProperty("agent_id") val agentId: UUID,
        @JsonProperty("operation_type") val operationType: String?,
        val context: JsonNode,
        val status: String,
        @JsonProperty("requested_at") val requestedAt: Instant,
        @JsonProperty("expires_at") val expiresAt: Instant,
        @JsonProperty("resolved_at") val resolvedAt: Instant?,
    )

    @JsonInclude(JsonInclude.Include.ALWAYS)
    data class ApprovalListResponse(val items: List<ApprovalView>)

    data class ResolveBody(val note: String? = null)

    data class HilConfigBody(
        @JsonProperty("default_mode") val defaultMode: String? = null,
        @JsonProperty("ask_on_triggers") val askOnTriggers: List<String>? = null,
    )

    data class HilConfigView(
        @JsonProperty("agent_id") val agentId: UUID,
        @JsonProperty("default_mode") val defaultMode: String,
        @JsonProperty("ask_on_triggers") val askOnTriggers: List<String>,
    )

    // ── Agent (requester) ─────────────────────────────────────────────────────────────────────
    @PostMapping("/v1/hil/approvals/request")
    fun requestApproval(@RequestBody body: RequestApprovalBody): AskResponse {
        val caller = callerContext.current()
        val agentId = caller.agentId
            ?: throw ApiException.forbidden("Caller is not an agent")
        val opType = body.operationType?.trim()?.takeIf { it.isNotEmpty() }
            ?: throw ApiException.validation("operation_type is required", mapOf("field" to "operation_type"))
        val contextJson = body.context?.let {
            if (!it.isObject) throw ApiException.validation("context must be a JSON object")
            objectMapper.writeValueAsString(it)
        } ?: "{}"
        val result = hilService.requestApproval(caller.tenantId, agentId, opType, contextJson)
        return AskResponse(result.autoApproved, result.requestId, result.status)
    }

    @GetMapping("/v1/hil/approvals/{requestId}")
    fun getApproval(@PathVariable requestId: UUID): ApprovalView {
        val caller = callerContext.current()
        return hilService.getStatus(caller.tenantId, requestId).toView()
    }

    // ── Human (approver — scope hil:approve) ───────────────────────────────────────────────────
    @GetMapping("/v1/hil/approvals")
    fun listApprovals(@RequestParam(required = false) operation_type: String?): ApprovalListResponse {
        val caller = callerContext.requireAnyScope("hil:approve", "tenant:admin", "platform:admin")
        return ApprovalListResponse(
            hilService.listPending(caller.tenantId, operation_type).map { it.toView() },
        )
    }

    @PostMapping("/v1/hil/approvals/{requestId}/grant")
    fun grant(@PathVariable requestId: UUID, @RequestBody(required = false) body: ResolveBody?): ApprovalView {
        val caller = callerContext.requireAnyScope("hil:approve", "tenant:admin", "platform:admin")
        val by = caller.agentId ?: ai.cypherx.auth.domain.SYSTEM_USER_ID
        return hilService.grant(caller.tenantId, requestId, by, body?.note).toView()
    }

    @PostMapping("/v1/hil/approvals/{requestId}/deny")
    fun deny(@PathVariable requestId: UUID, @RequestBody(required = false) body: ResolveBody?): ApprovalView {
        val caller = callerContext.requireAnyScope("hil:approve", "tenant:admin", "platform:admin")
        val by = caller.agentId ?: ai.cypherx.auth.domain.SYSTEM_USER_ID
        return hilService.deny(caller.tenantId, requestId, by, body?.note).toView()
    }

    // ── Orchestrator HIL config ─────────────────────────────────────────────────────────────────
    @GetMapping("/v1/orchestrator/hil-config")
    fun getConfig(): HilConfigView {
        val caller = callerContext.requireAnyScope("orchestrator:manage", "tenant:admin", "platform:admin")
        val orchestratorId = caller.agentId
            ?: throw ApiException.forbidden("Caller is not an agent")
        val cfg = hilService.getConfig(caller.tenantId, orchestratorId)
        return HilConfigView(cfg.agentId, cfg.defaultMode, cfg.askOnTriggers)
    }

    @PutMapping("/v1/orchestrator/hil-config")
    fun putConfig(@RequestBody body: HilConfigBody): HilConfigView {
        val caller = callerContext.requireAnyScope("orchestrator:manage", "tenant:admin", "platform:admin")
        val orchestratorId = caller.agentId
            ?: throw ApiException.forbidden("Caller is not an agent")
        val mode = body.defaultMode?.trim()
            ?: throw ApiException.validation("default_mode is required", mapOf("field" to "default_mode"))
        val cfg = hilService.setConfig(caller.tenantId, orchestratorId, mode, body.askOnTriggers ?: emptyList())
        return HilConfigView(cfg.agentId, cfg.defaultMode, cfg.askOnTriggers)
    }

    private fun ApprovalRequestRow.toView() = ApprovalView(
        requestId = requestId,
        agentId = agentId,
        operationType = operationType,
        context = runCatching { objectMapper.readTree(operationContextJson) }
            .getOrElse { objectMapper.readTree("{}") },
        status = status,
        requestedAt = requestedAt,
        expiresAt = expiresAt,
        resolvedAt = resolvedAt,
    )
}
