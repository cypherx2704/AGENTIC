package ai.cypherx.auth.api

import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.QuotaResolution
import ai.cypherx.auth.service.QuotaService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.JsonNode
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PutMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Quota / effective-limits API (Component 1d / Contract 19).
 *
 *  Service / platform read (the document other services consume before enforcing a limit):
 *    GET /v1/tenants/{id}/limits           effective limits for tenant {id}
 *                                          (scope `internal:read` — service token — or `platform:admin`)
 *
 *  Self-service read (the CALLER's own tenant, taken from the verified JWT):
 *    GET /v1/quotas                        effective limits for the caller's tenant
 *                                          (scope `tenant:read`, or any admin scope)
 *
 *  Platform-admin (raw override + plan + effective; set an override):
 *    GET /v1/admin/tenants/{id}/quotas     plan + raw override + effective (scope `platform:admin`)
 *    PUT /v1/admin/tenants/{id}/quotas     set an override (body = a limits patch) (scope `platform:admin`)
 *
 * Scope enforcement is in-handler via [CallerContext.requireAnyScope] (method-level security is not
 * enabled in the locked SecurityConfig). The Core [ai.cypherx.auth.web.AgentJwtAuthFilter] attaches
 * `SCOPE_*` authorities and SecurityConfig already requires an authenticated principal for every
 * route here. Errors are thrown as [ApiException] → rendered by the Core GlobalExceptionHandler.
 */
@RestController
@RequestMapping("/v1")
class QuotaController(
    private val quotaService: QuotaService,
    private val callerContext: CallerContext,
) {

    // ── Service / platform: effective limits for an arbitrary tenant ─────────────────────────

    @GetMapping("/tenants/{tenantId}/limits")
    fun limitsForTenant(@PathVariable tenantId: UUID): JsonNode {
        // Cross-service read: a platform service token (internal:read) or a platform admin.
        callerContext.requireAnyScope(SCOPE_INTERNAL_READ, SCOPE_PLATFORM_ADMIN)
        return quotaService.effectiveLimits(tenantId)
    }

    // ── Self-service: effective limits for the caller's own tenant ───────────────────────────

    @GetMapping("/quotas")
    fun myQuotas(): JsonNode {
        val caller = callerContext.requireAnyScope(
            SCOPE_TENANT_READ, SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN,
        )
        return quotaService.effectiveLimits(caller.tenantId)
    }

    // ── Platform-admin: raw override view + set override ─────────────────────────────────────

    @GetMapping("/admin/tenants/{tenantId}/quotas")
    fun adminGetQuotas(@PathVariable tenantId: UUID): Map<String, Any?> {
        callerContext.requireAnyScope(SCOPE_PLATFORM_ADMIN)
        return toAdminView(quotaService.resolve(tenantId))
    }

    @PutMapping("/admin/tenants/{tenantId}/quotas")
    fun adminSetOverride(
        @PathVariable tenantId: UUID,
        @RequestBody(required = false) body: SetQuotaOverrideRequest?,
    ): Map<String, Any?> {
        val caller = callerContext.requireAnyScope(SCOPE_PLATFORM_ADMIN)
        val patch = body?.limits
            ?: throw ApiException.validation(
                "Request body must contain a 'limits' object",
                mapOf("field" to "limits"),
            )
        if (!patch.isObject) {
            throw ApiException.validation("'limits' must be a JSON object", mapOf("field" to "limits"))
        }
        val updatedBy = caller.subject ?: caller.agentId?.toString() ?: caller.tenantId.toString()
        return toAdminView(quotaService.setOverride(tenantId, patch, updatedBy))
    }

    // ── View mapping ─────────────────────────────────────────────────────────────────────────

    /** Admin view: the plan, the plan-default base, the raw override (+ provenance), and effective. */
    private fun toAdminView(r: QuotaResolution): Map<String, Any?> = linkedMapOf(
        "tenant_id" to r.tenantId.toString(),
        "plan" to r.plan,
        "plan_defaults" to r.planDefaults,
        "override" to r.override,
        "override_source" to r.overrideSource,
        "override_updated_by" to r.overrideUpdatedBy,
        "override_effective_from" to r.overrideEffectiveFrom?.let(TIMESTAMP_FMT::format),
        "effective" to r.effective,
    )

    /** Inbound body for `PUT /v1/admin/tenants/{id}/quotas`: a partial limits document. */
    data class SetQuotaOverrideRequest(val limits: JsonNode? = null)

    private companion object {
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
        const val SCOPE_TENANT_READ = "tenant:read"
        const val SCOPE_TENANT_ADMIN = "tenant:admin"
        const val SCOPE_INTERNAL_READ = "internal:read"
        val TIMESTAMP_FMT: DateTimeFormatter = DateTimeFormatter.ISO_INSTANT
    }
}
