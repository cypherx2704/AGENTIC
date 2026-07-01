package ai.cypherx.auth.api

import ai.cypherx.auth.repo.AgentRecord
import ai.cypherx.auth.service.AgentService
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.OrchestratorService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.annotation.JsonInclude
import com.fasterxml.jackson.annotation.JsonProperty
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.web.bind.annotation.DeleteMapping
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PatchMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import java.time.Instant
import java.util.UUID

/**
 * Orchestrator-only sub-agent management (the agent hierarchy surface):
 *
 *  - `POST   /v1/orchestrator/sub-agents`         create a sub-agent  [scope: orchestrator:manage]
 *  - `GET    /v1/orchestrator/sub-agents`         list own sub-agents [scope: orchestrator:manage]
 *  - `PATCH  /v1/orchestrator/sub-agents/{id}`    update own sub-agent [scope: orchestrator:manage]
 *  - `DELETE /v1/orchestrator/sub-agents/{id}`    deactivate own sub-agent [scope: orchestrator:manage]
 *
 * The `orchestrator:manage` scope is the first gate; [OrchestratorService] then verifies the caller
 * is genuinely the tenant orchestrator (DB agent_type) and enforces the subset-scope + ownership
 * rules. Identity comes from the verified JWT via [CallerContext].
 */
@RestController
class OrchestratorController(
    private val orchestratorService: OrchestratorService,
    private val callerContext: CallerContext,
    private val objectMapper: ObjectMapper,
) {

    data class CreateSubAgentRequest(
        val name: String? = null,
        val version: String? = null,
        @JsonProperty("allowed_scopes") val allowedScopes: List<String>? = null,
    )

    data class UpdateSubAgentRequest(
        @JsonProperty("allowed_scopes") val allowedScopes: List<String>? = null,
        val capabilities: JsonNode? = null,
        val metadata: JsonNode? = null,
    )

    data class AgentResponse(
        @JsonProperty("agent_id") val agentId: UUID,
        @JsonProperty("tenant_id") val tenantId: UUID,
        val name: String,
        val version: String,
        val status: String,
        @JsonProperty("agent_type") val agentType: String,
        @JsonProperty("parent_orchestrator_id") val parentOrchestratorId: UUID?,
        @JsonProperty("immutable_llm") val immutableLlm: Boolean,
        @JsonProperty("allowed_scopes") val allowedScopes: List<String>,
        val capabilities: JsonNode,
        val metadata: JsonNode,
        @JsonProperty("created_at") val createdAt: Instant,
        @JsonProperty("updated_at") val updatedAt: Instant,
    )

    @JsonInclude(JsonInclude.Include.ALWAYS)
    data class SubAgentListResponse(
        val items: List<AgentResponse>,
        @JsonProperty("next_cursor") val nextCursor: String?,
    )

    data class DeactivateResponse(
        val agent: AgentResponse,
        @JsonProperty("keys_revoked") val keysRevoked: Int,
        @JsonProperty("tokens_revoked") val tokensRevoked: Int,
    )

    @PostMapping("/v1/orchestrator/sub-agents")
    fun createSubAgent(@RequestBody request: CreateSubAgentRequest): ResponseEntity<AgentResponse> {
        val caller = callerContext.requireAnyScope("orchestrator:manage").toAgentCaller()
        val name = request.name?.trim()
        if (name.isNullOrEmpty()) {
            throw ApiException.validation("name is required", mapOf("field" to "name"))
        }
        val agent = orchestratorService.createSubAgent(
            caller = caller,
            name = name,
            version = request.version?.trim().orEmpty().ifEmpty { "1.0.0" },
            requestedScopes = request.allowedScopes ?: emptyList(),
        )
        return ResponseEntity.status(HttpStatus.CREATED).body(agent.toResponse())
    }

    @GetMapping("/v1/orchestrator/sub-agents")
    fun listSubAgents(
        @RequestParam(required = false) cursor: String?,
        @RequestParam(required = false, defaultValue = "50") limit: Int,
    ): SubAgentListResponse {
        val caller = callerContext.requireAnyScope("orchestrator:manage").toAgentCaller()
        val page = orchestratorService.listSubAgents(caller, cursor, limit)
        return SubAgentListResponse(items = page.agents.map { it.toResponse() }, nextCursor = page.nextCursor)
    }

    @PatchMapping("/v1/orchestrator/sub-agents/{subAgentId}")
    fun updateSubAgent(
        @PathVariable subAgentId: UUID,
        @RequestBody request: UpdateSubAgentRequest,
    ): AgentResponse {
        val caller = callerContext.requireAnyScope("orchestrator:manage").toAgentCaller()
        if (request.allowedScopes == null && request.capabilities == null && request.metadata == null) {
            throw ApiException.validation(
                "At least one of allowed_scopes, capabilities, metadata must be supplied",
                mapOf("fields" to listOf("allowed_scopes", "capabilities", "metadata")),
            )
        }
        val capabilitiesJson = request.capabilities?.let {
            if (!it.isArray) throw ApiException.validation("capabilities must be a JSON array", mapOf("field" to "capabilities"))
            objectMapper.writeValueAsString(it)
        }
        val metadataJson = request.metadata?.let {
            if (!it.isObject) throw ApiException.validation("metadata must be a JSON object", mapOf("field" to "metadata"))
            objectMapper.writeValueAsString(it)
        }
        return orchestratorService.updateSubAgent(
            caller = caller,
            subAgentId = subAgentId,
            allowedScopes = request.allowedScopes,
            capabilitiesJson = capabilitiesJson,
            metadataJson = metadataJson,
        ).toResponse()
    }

    @DeleteMapping("/v1/orchestrator/sub-agents/{subAgentId}")
    fun deleteSubAgent(@PathVariable subAgentId: UUID): DeactivateResponse {
        val caller = callerContext.requireAnyScope("orchestrator:manage").toAgentCaller()
        val result = orchestratorService.deactivateSubAgent(caller, subAgentId)
        return DeactivateResponse(
            agent = result.agent.toResponse(),
            keysRevoked = result.keysRevoked,
            tokensRevoked = result.tokensRevoked,
        )
    }

    /** Map the CallerContext identity + Security authorities into an AgentService.Caller. */
    private fun CallerContext.Caller.toAgentCaller(): AgentService.Caller {
        val auth = SecurityContextHolder.getContext().authentication
            ?: throw ApiException.unauthorized("Authentication required")
        val scopes = auth.authorities
            .map { it.authority }
            .filter { it.startsWith("SCOPE_") }
            .map { it.removePrefix("SCOPE_") }
            .toSet()
        return AgentService.Caller(agentId = agentId, tenantId = tenantId, scopes = scopes)
    }

    private fun AgentRecord.toResponse() = AgentResponse(
        agentId = agentId,
        tenantId = tenantId,
        name = name,
        version = version,
        status = status,
        agentType = agentType,
        parentOrchestratorId = parentOrchestratorId,
        immutableLlm = immutableLlm,
        allowedScopes = allowedScopes,
        capabilities = parseJsonOrEmpty(capabilities, "[]"),
        metadata = parseJsonOrEmpty(metadata, "{}"),
        createdAt = createdAt,
        updatedAt = updatedAt,
    )

    private fun parseJsonOrEmpty(json: String, fallback: String): JsonNode =
        runCatching { objectMapper.readTree(json) }.getOrElse { objectMapper.readTree(fallback) }
}
