package ai.cypherx.auth.api

import ai.cypherx.auth.repo.AgentRecord
import ai.cypherx.auth.service.AgentService
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.annotation.JsonInclude
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
 * Agent registry HTTP surface (Phase 2 Component 1):
 *
 *  - `POST   /v1/agents`              register an agent   [scope: platform:admin OR tenant:admin]
 *  - `GET    /v1/agents`              list tenant agents  [scope: agent:read OR tenant/platform:admin]
 *  - `GET    /v1/agents/{id}`         read an agent        [any authenticated agent JWT]
 *  - `PATCH  /v1/agents/{id}`         partial update       [scope: agent:write OR tenant/platform:admin]
 *  - `DELETE /v1/agents/{id}`         deactivate + cascade [scope: agent:write OR tenant/platform:admin]
 *  - `POST   /v1/agents/{id}/deactivate`  (alias of DELETE)
 *
 * All routes are authenticated-by-default (SecurityConfig `anyRequest().authenticated()`); the
 * [ai.cypherx.auth.web.AgentJwtAuthFilter] establishes the principal + SCOPE_* authorities. Method
 * security is not enabled service-wide, so the create-scope check is enforced here against the
 * authenticated authorities. The caller's tenant/agent identity comes from the shared
 * [CallerContext] (re-verifies the bearer token to read the `tenant_id` claim the filter omits).
 *
 * Errors are thrown as [ApiException] and rendered by the Core GlobalExceptionHandler.
 */
@RestController
class AgentController(
    private val agentService: AgentService,
    private val callerContext: CallerContext,
    private val objectMapper: ObjectMapper,
) {

    /** Create-agent request body. */
    data class CreateAgentRequest(
        val name: String?,
        val version: String? = null,
        val allowedScopes: List<String>? = null,
        /** Optional target tenant (platform:admin only when different from caller's tenant). */
        val tenantId: UUID? = null,
    )

    /** Agent response view (no secrets). */
    data class AgentResponse(
        val agentId: UUID,
        val tenantId: UUID,
        val name: String,
        val version: String,
        val status: String,
        val allowedScopes: List<String>,
        val capabilities: JsonNode,
        val metadata: JsonNode,
        val createdBy: UUID,
        val createdAt: Instant,
        val updatedAt: Instant,
    )

    /** A page of agents plus the opaque cursor for the next page (null at end of list). */
    @JsonInclude(JsonInclude.Include.ALWAYS)
    data class AgentListResponse(
        val items: List<AgentResponse>,
        val nextCursor: String?,
    )

    /**
     * Partial-update body. Every field is OPTIONAL; an absent field is left unchanged. `capabilities`
     * MUST be a JSON array and `metadata` a JSON object when present (validated here). At least one
     * field must be supplied.
     */
    data class UpdateAgentRequest(
        val allowedScopes: List<String>? = null,
        val capabilities: JsonNode? = null,
        val metadata: JsonNode? = null,
    )

    /** Result of a deactivate cascade. */
    data class DeactivateResponse(
        val agent: AgentResponse,
        val keysRevoked: Int,
        val tokensRevoked: Int,
    )

    @PostMapping("/v1/agents")
    fun createAgent(@RequestBody request: CreateAgentRequest): ResponseEntity<AgentResponse> {
        val caller = resolveCaller()
        if (!caller.isPlatformAdmin && !caller.isTenantAdmin) {
            throw ApiException.forbidden(
                "Creating an agent requires platform:admin or tenant:admin scope",
                mapOf("required" to listOf("platform:admin", "tenant:admin")),
            )
        }

        val name = request.name?.trim()
        if (name.isNullOrEmpty()) {
            throw ApiException.validation("name is required", mapOf("field" to "name"))
        }

        val command = AgentService.CreateAgentCommand(
            name = name,
            version = request.version ?: "",
            allowedScopes = request.allowedScopes ?: emptyList(),
            requestedTenantId = request.tenantId,
        )
        val agent = agentService.createAgent(command, caller)
        return ResponseEntity.status(HttpStatus.CREATED).body(agent.toResponse())
    }

    @GetMapping("/v1/agents")
    fun listAgents(
        @RequestParam(required = false) status: String?,
        @RequestParam(required = false) name: String?,
        @RequestParam(required = false) cursor: String?,
        @RequestParam(required = false, defaultValue = "50") limit: Int,
    ): AgentListResponse {
        val caller = resolveCaller()
        requireAnyScope(caller, "agent:read", "tenant:admin", "platform:admin")
        val page = agentService.listAgents(
            caller = caller,
            statusFilter = status,
            nameContains = name,
            cursor = cursor,
            limit = limit,
        )
        return AgentListResponse(items = page.agents.map { it.toResponse() }, nextCursor = page.nextCursor)
    }

    @GetMapping("/v1/agents/{agentId}")
    fun getAgent(@PathVariable agentId: UUID): AgentResponse {
        val caller = resolveCaller()
        return agentService.getAgent(agentId, caller).toResponse()
    }

    @PatchMapping("/v1/agents/{agentId}")
    fun updateAgent(
        @PathVariable agentId: UUID,
        @RequestBody request: UpdateAgentRequest,
    ): AgentResponse {
        val caller = resolveCaller()
        requireAnyScope(caller, "agent:write", "tenant:admin", "platform:admin")

        if (request.allowedScopes == null && request.capabilities == null && request.metadata == null) {
            throw ApiException.validation(
                "At least one of allowed_scopes, capabilities, metadata must be supplied",
                mapOf("fields" to listOf("allowed_scopes", "capabilities", "metadata")),
            )
        }
        // capabilities MUST be a JSON array; metadata MUST be a JSON object (stored as JSONB).
        val capabilitiesJson = request.capabilities?.let {
            if (!it.isArray) {
                throw ApiException.validation("capabilities must be a JSON array", mapOf("field" to "capabilities"))
            }
            objectMapper.writeValueAsString(it)
        }
        val metadataJson = request.metadata?.let {
            if (!it.isObject) {
                throw ApiException.validation("metadata must be a JSON object", mapOf("field" to "metadata"))
            }
            objectMapper.writeValueAsString(it)
        }

        val command = AgentService.UpdateAgentCommand(
            allowedScopes = request.allowedScopes,
            capabilitiesJson = capabilitiesJson,
            metadataJson = metadataJson,
        )
        return agentService.updateAgent(agentId, command, caller).toResponse()
    }

    @DeleteMapping("/v1/agents/{agentId}")
    fun deleteAgent(@PathVariable agentId: UUID): DeactivateResponse = deactivate(agentId)

    @PostMapping("/v1/agents/{agentId}/deactivate")
    fun deactivateAgent(@PathVariable agentId: UUID): DeactivateResponse = deactivate(agentId)

    private fun deactivate(agentId: UUID): DeactivateResponse {
        val caller = resolveCaller()
        requireAnyScope(caller, "agent:write", "tenant:admin", "platform:admin")
        val result = agentService.deactivateAgent(agentId, caller)
        return DeactivateResponse(
            agent = result.agent.toResponse(),
            keysRevoked = result.keysRevoked,
            tokensRevoked = result.tokensRevoked,
        )
    }

    /**
     * Build the [AgentService.Caller] for the current request: tenant/agent from the shared
     * [CallerContext] (verified JWT), scopes from the Spring Security authorities (SCOPE_*).
     */
    private fun resolveCaller(): AgentService.Caller {
        val identity = callerContext.current()
        val auth = SecurityContextHolder.getContext().authentication
            ?: throw ApiException.unauthorized("Authentication required")
        val scopes = auth.authorities
            .map { it.authority }
            .filter { it.startsWith(SCOPE_PREFIX) }
            .map { it.removePrefix(SCOPE_PREFIX) }
            .toSet()
        return AgentService.Caller(
            agentId = identity.agentId,
            tenantId = identity.tenantId,
            scopes = scopes,
        )
    }

    private fun AgentRecord.toResponse() = AgentResponse(
        agentId = agentId,
        tenantId = tenantId,
        name = name,
        version = version,
        status = status,
        allowedScopes = allowedScopes,
        capabilities = parseJsonOrEmpty(capabilities, "[]"),
        metadata = parseJsonOrEmpty(metadata, "{}"),
        createdBy = createdBy,
        createdAt = createdAt,
        updatedAt = updatedAt,
    )

    /** Parse stored JSONB text back into a node; fall back to [fallback] if it is ever unparsable. */
    private fun parseJsonOrEmpty(json: String, fallback: String): JsonNode =
        runCatching { objectMapper.readTree(json) }.getOrElse { objectMapper.readTree(fallback) }

    /**
     * Programmatic scope guard mirroring the other controllers (method-security is not enabled in the
     * locked SecurityConfig). A `platform:admin` token implicitly satisfies any required scope; the
     * caller's scopes come from the SCOPE_* authorities the agent-JWT filter attached. 403 otherwise.
     */
    private fun requireAnyScope(caller: AgentService.Caller, vararg required: String) {
        if (caller.scopes.contains("platform:admin")) return
        if (required.any { caller.scopes.contains(it) }) return
        throw ApiException.forbidden(
            "Caller lacks the required scope",
            mapOf("required_any" to required.toList()),
        )
    }

    private companion object {
        const val SCOPE_PREFIX = "SCOPE_"
    }
}
