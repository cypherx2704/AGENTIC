package ai.cypherx.auth.api

import ai.cypherx.auth.repo.AuditRepository
import ai.cypherx.auth.service.AuditService
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.ApiException
import org.springframework.format.annotation.DateTimeFormat
import org.springframework.http.ResponseEntity
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import java.time.Instant
import java.util.UUID

/**
 * Audit-log read & chain-verification API (Component 6, Phase 2):
 *
 *   GET /v1/audit-log         — cursor-paginated read of the caller tenant's audit rows.
 *   GET /v1/audit-log/verify  — re-walk the per-tenant tamper-evident hash chain over a window.
 *
 * Required scope: `tenant:admin` (own tenant) OR `platform:admin` (any tenant via `tenant_id`).
 * The caller's tenant is taken from the verified JWT (Contract 13); a `platform:admin` may target
 * another tenant via the `tenant_id` query param. RLS in [AuditRepository] enforces the boundary.
 *
 * Method-security is not enabled in Core, so the scope check is performed here against the
 * SecurityContext authorities attached by [ai.cypherx.auth.web.AgentJwtAuthFilter].
 */
@RestController
@RequestMapping("/v1/audit-log")
class AuditController(
    private val auditService: AuditService,
    private val jwtMintService: JwtMintService,
) {

    @GetMapping
    fun list(
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) from: Instant?,
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) to: Instant?,
        @RequestParam(name = "event_type", required = false) eventType: String?,
        @RequestParam(name = "agent_id", required = false) agentId: String?,
        @RequestParam(required = false) cursor: String?,
        @RequestParam(required = false, defaultValue = "50") limit: Int,
        @RequestParam(name = "tenant_id", required = false) tenantIdParam: String?,
    ): ResponseEntity<Map<String, Any?>> {
        val tenantId = resolveTenant(tenantIdParam)
        val agentUuid = agentId?.let { parseUuid(it, "agent_id") }
        val afterId = cursor?.takeIf { it.isNotBlank() }?.let {
            it.toLongOrNull() ?: throw ApiException.validation("Invalid cursor", mapOf("field" to "cursor"))
        }

        val rows = auditService.list(
            tenantId = tenantId,
            from = from,
            to = to,
            eventType = eventType,
            agentId = agentUuid,
            afterId = afterId,
            limit = limit,
        )
        val items = rows.map { it.toResponse() }
        val nextCursor = if (rows.size >= limit.coerceIn(1, 500)) rows.lastOrNull()?.id?.toString() else null
        return ResponseEntity.ok(mapOf("items" to items, "next_cursor" to nextCursor))
    }

    @GetMapping("/verify")
    fun verify(
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) from: Instant?,
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) to: Instant?,
        @RequestParam(name = "tenant_id", required = false) tenantIdParam: String?,
    ): ResponseEntity<Map<String, Any?>> {
        val tenantId = resolveTenant(tenantIdParam)
        val result = auditService.verifyChain(tenantId, from, to)
        val body: Map<String, Any?> = if (result.ok) {
            mapOf(
                "ok" to true,
                "rows_verified" to result.rowsVerified,
                "from_hash" to result.fromHashHex,
                "to_hash" to result.toHashHex,
            )
        } else {
            mapOf(
                "ok" to false,
                "broken_at_row_id" to result.brokenAtRowId,
                "expected_prev_hash" to result.expectedPrevHashHex,
                "actual_prev_hash" to result.actualPrevHashHex,
            )
        }
        return ResponseEntity.ok(body)
    }

    // ── helpers ────────────────────────────────────────────────────────────────────────────

    /**
     * Resolve the tenant to read: a `platform:admin` may pass `tenant_id` to read any tenant; a
     * `tenant:admin` may only read its own (the `tenant_id` claim from its JWT). Anyone else → 403.
     */
    private fun resolveTenant(tenantIdParam: String?): UUID {
        val auth = SecurityContextHolder.getContext().authentication
            ?: throw ApiException.unauthorized("Authentication required")
        val authorities = auth.authorities.map { it.authority }.toSet()
        val isPlatformAdmin = authorities.contains("SCOPE_platform:admin")
        val isTenantAdmin = authorities.contains("SCOPE_tenant:admin")
        if (!isPlatformAdmin && !isTenantAdmin) {
            throw ApiException.forbidden(
                "Caller lacks audit-read scope",
                mapOf("required_any" to listOf("tenant:admin", "platform:admin")),
            )
        }

        val rawToken = auth.credentials as? String ?: throw ApiException.unauthorized("Caller token unavailable")
        val claims = jwtMintService.verify(rawToken)?.jwtClaimsSet ?: throw ApiException.unauthorized("Caller token invalid")
        val ownTenant = claims.getStringClaim("tenant_id")?.let { runCatching { UUID.fromString(it) }.getOrNull() }
            ?: throw ApiException.forbidden("Caller token has no tenant_id")

        if (tenantIdParam.isNullOrBlank()) return ownTenant
        val requested = parseUuid(tenantIdParam, "tenant_id")
        if (requested != ownTenant && !isPlatformAdmin) {
            throw ApiException.forbidden("Only platform:admin may read another tenant's audit log")
        }
        return requested
    }

    private fun parseUuid(value: String, field: String): UUID =
        runCatching { UUID.fromString(value) }.getOrElse {
            throw ApiException.validation("Invalid UUID for $field", mapOf("field" to field))
        }

    private fun AuditRepository.AuditRow.toResponse(): Map<String, Any?> = linkedMapOf(
        "id" to id,
        "event_type" to eventType,
        "agent_id" to agentId?.toString(),
        "tenant_id" to tenantId.toString(),
        "action" to action,
        "resource" to resource,
        "decision" to decision,
        "policy_ids" to policyIds,
        "request_id" to requestId?.toString(),
        "trace_id" to traceId?.toString(),
        "ip_address" to ipAddress,
        "created_at" to createdAt.toString(),
        "row_hash" to rowHash.toHex(),
        "prev_row_hash" to prevRowHash.toHex(),
    )

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }
}
