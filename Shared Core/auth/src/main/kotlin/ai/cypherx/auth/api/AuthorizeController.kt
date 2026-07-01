package ai.cypherx.auth.api

import ai.cypherx.auth.service.AuthorizeService
import ai.cypherx.auth.web.ApiException
import jakarta.servlet.http.HttpServletRequest
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestHeader
import org.springframework.web.bind.annotation.RestController

/**
 * Component 4 — `POST /v1/authorize`. The single endpoint every service calls before performing a
 * protected action (Phase 2 Component 4).
 *
 * Contract 13 anti-pattern enforcement: the agent identity and tenant come ONLY from the
 * `X-Forwarded-Agent-JWT` header. If the body carries `agent_id` or `tenant_id`, the request is
 * rejected with 400 BAD_REQUEST — a service must never assert another tenant's/agent's identity
 * via the body.
 *
 * Request body: `{ "action": "...", "resource": "...", "context": { ... } }` (resource and context
 * optional). Response: `{ "allowed": bool, "reason": string|null, "policy_ids": [..] }`.
 *
 * The calling service authenticates itself to Auth with its own service token (`Authorization:
 * Bearer ...`), satisfying the `anyRequest authenticated` rule in SecurityConfig; this endpoint
 * then authorizes the *forwarded agent*, not the caller.
 */
@RestController
class AuthorizeController(
    private val authorizeService: AuthorizeService,
) {

    @PostMapping("/v1/authorize")
    fun authorize(
        @RequestHeader(value = HDR_FORWARDED_AGENT_JWT, required = false) forwardedAgentJwt: String?,
        @RequestBody(required = false) body: Map<String, Any?>?,
        request: HttpServletRequest,
    ): ResponseEntity<Map<String, Any?>> {
        val jwt = forwardedAgentJwt?.trim()?.takeIf { it.isNotEmpty() }
            ?: throw ApiException.unauthorized(
                "Missing required header $HDR_FORWARDED_AGENT_JWT",
                mapOf("header" to HDR_FORWARDED_AGENT_JWT),
            )

        val payload = body ?: emptyMap()

        // Contract 13 anti-pattern: identity must come from the JWT, never the body.
        val forbidden = IDENTITY_KEYS.filter { payload.containsKey(it) }
        if (forbidden.isNotEmpty()) {
            throw ApiException(
                code = "VALIDATION_ERROR",
                httpStatus = HttpStatus.BAD_REQUEST,
                message = "Request body must not contain ${forbidden.joinToString(" or ")}; " +
                    "agent_id and tenant_id are taken from $HDR_FORWARDED_AGENT_JWT",
                details = mapOf("forbidden_fields" to forbidden),
            )
        }

        val action = (payload["action"] as? String)?.trim()?.takeIf { it.isNotEmpty() }
            ?: throw ApiException.validation(
                "Request body field 'action' is required",
                mapOf("field" to "action"),
            )
        val resource = (payload["resource"] as? String)?.trim()?.takeIf { it.isNotEmpty() }

        @Suppress("UNCHECKED_CAST")
        val context = (payload["context"] as? Map<String, Any?>) ?: emptyMap()

        val decision = authorizeService.authorize(
            forwardedAgentJwt = jwt,
            action = action,
            resource = resource,
            context = context,
            ipAddress = clientIp(request),
        )

        // Exact wire shape: { allowed, reason, policy_ids }.
        val responseBody = linkedMapOf<String, Any?>(
            "allowed" to decision.allowed,
            "reason" to decision.reason,
            "policy_ids" to decision.policyIds,
        )
        return ResponseEntity.ok(responseBody)
    }

    /** Best-effort client IP for the audit row: X-Forwarded-For first hop, else remote addr. */
    private fun clientIp(request: HttpServletRequest): String? =
        request.getHeader("X-Forwarded-For")
            ?.split(",")
            ?.firstOrNull()
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
            ?: request.remoteAddr

    private companion object {
        const val HDR_FORWARDED_AGENT_JWT = "X-Forwarded-Agent-JWT"
        val IDENTITY_KEYS = listOf("agent_id", "tenant_id")
    }
}
