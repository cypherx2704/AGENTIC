package ai.cypherx.auth.api

import ai.cypherx.auth.service.AuditExportService
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
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Audit-log EXPORT API (Component 6 export — WP04). Distinct from [AuditController] (the read/verify
 * API): this controller streams a tenant's full `auth.audit_log` to object storage and returns a
 * presigned download URL.
 *
 *   GET /v1/audit-log/export   — export the caller tenant's audit log as JSONL to object storage,
 *                                returning a presigned URL (Contract default TTL = 7 days).
 *
 * Required scope: `tenant:admin` (own tenant) OR `platform:admin` (any tenant via `tenant_id`),
 * matching [AuditController]'s authorization model. The caller's tenant is taken from the verified
 * JWT (Contract 13); a `platform:admin` may target another tenant via the `tenant_id` query param.
 * RLS in the read path enforces the boundary.
 *
 * Method-security is not enabled in Core, so the scope check is performed here against the
 * SecurityContext authorities attached by [ai.cypherx.auth.web.AgentJwtAuthFilter]. Errors are thrown
 * as [ApiException] → rendered by the Core GlobalExceptionHandler (Contract 2 envelope).
 */
@RestController
@RequestMapping("/v1/audit-log")
class AuditExportController(
    private val auditExportService: AuditExportService,
    private val jwtMintService: JwtMintService,
) {

    @GetMapping("/export")
    fun export(
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) from: Instant?,
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) to: Instant?,
        @RequestParam(name = "tenant_id", required = false) tenantIdParam: String?,
    ): ResponseEntity<Map<String, Any?>> {
        val resolved = resolveCaller(tenantIdParam)
        val result = auditExportService.export(resolved.tenantId, from, to, resolved.agentId)
        return ResponseEntity.ok(
            linkedMapOf(
                "export_id" to result.exportId?.toString(),
                "tenant_id" to result.tenantId.toString(),
                "object_key" to result.objectKey,
                "object_uri" to result.objectUri,
                "row_count" to result.rowCount,
                "truncated" to result.truncated,
                "download_url" to result.downloadUrl,
                "expires_at" to TIMESTAMP_FMT.format(result.expiresAt),
                "store" to result.backend,
            ),
        )
    }

    // ── helpers (mirror AuditController.resolveTenant) ──────────────────────────────────────────

    /** The resolved export target tenant + the acting admin agent id (for the audit-trail row). */
    private data class ResolvedCaller(val tenantId: UUID, val agentId: UUID?)

    /**
     * Resolve the tenant to export AND the acting agent: a `platform:admin` may pass `tenant_id` to
     * export any tenant; a `tenant:admin` may only export its own (the `tenant_id` claim from its
     * JWT). Anyone else → 403. The acting agent id is the verified token `sub`/`agent_id` claim.
     */
    private fun resolveCaller(tenantIdParam: String?): ResolvedCaller {
        val auth = SecurityContextHolder.getContext().authentication
            ?: throw ApiException.unauthorized("Authentication required")
        val authorities = auth.authorities.map { it.authority }.toSet()
        val isPlatformAdmin = authorities.contains("SCOPE_platform:admin")
        val isTenantAdmin = authorities.contains("SCOPE_tenant:admin")
        if (!isPlatformAdmin && !isTenantAdmin) {
            throw ApiException.forbidden(
                "Caller lacks audit-export scope",
                mapOf("required_any" to listOf("tenant:admin", "platform:admin")),
            )
        }

        val rawToken = auth.credentials as? String ?: throw ApiException.unauthorized("Caller token unavailable")
        val claims = jwtMintService.verify(rawToken)?.jwtClaimsSet ?: throw ApiException.unauthorized("Caller token invalid")
        val ownTenant = claims.getStringClaim("tenant_id")?.let { runCatching { UUID.fromString(it) }.getOrNull() }
            ?: throw ApiException.forbidden("Caller token has no tenant_id")
        val actingAgentId = (claims.subject ?: claims.getStringClaim("agent_id"))
            ?.let { runCatching { UUID.fromString(it) }.getOrNull() }

        val targetTenant = if (tenantIdParam.isNullOrBlank()) {
            ownTenant
        } else {
            val requested = parseUuid(tenantIdParam, "tenant_id")
            if (requested != ownTenant && !isPlatformAdmin) {
                throw ApiException.forbidden("Only platform:admin may export another tenant's audit log")
            }
            requested
        }
        return ResolvedCaller(tenantId = targetTenant, agentId = actingAgentId)
    }

    private fun parseUuid(value: String, field: String): UUID =
        runCatching { UUID.fromString(value) }.getOrElse {
            throw ApiException.validation("Invalid UUID for $field", mapOf("field" to field))
        }

    private companion object {
        val TIMESTAMP_FMT: DateTimeFormatter = DateTimeFormatter.ISO_INSTANT
    }
}
