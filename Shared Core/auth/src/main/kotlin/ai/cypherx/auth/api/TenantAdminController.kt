package ai.cypherx.auth.api

import ai.cypherx.auth.repo.Tenant
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.CreateTenantRequest
import ai.cypherx.auth.service.TenantPage
import ai.cypherx.auth.service.TenantService
import ai.cypherx.auth.service.UpdateTenantRequest
import ai.cypherx.auth.web.ApiException
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
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Tenant administration (Phase 2 Component 1b).
 *
 *  Platform-admin routes (require scope `platform:admin`):
 *    POST   /v1/admin/tenants                       create
 *    GET    /v1/admin/tenants                       list (cursor pagination — Contract 9)
 *    GET    /v1/admin/tenants/{tenant_id}           get
 *    PATCH  /v1/admin/tenants/{tenant_id}/suspend   suspend
 *    PATCH  /v1/admin/tenants/{tenant_id}/resume    resume
 *    DELETE /v1/admin/tenants/{tenant_id}           soft-delete
 *
 *  Self-service routes (operate on the caller's OWN tenant, read from the JWT):
 *    GET    /v1/tenants/me                           current tenant info  (scope `tenant:read`)
 *    PATCH  /v1/tenants/me                           update own tenant    (scope `tenant:admin`)
 *
 * Scope enforcement is done in-controller (the Core [ai.cypherx.auth.web.AgentJwtAuthFilter]
 * attaches `SCOPE_*` authorities and swallows bad tokens; SecurityConfig requires an authenticated
 * principal for everything except the explicit permit-all list, so these routes already require a
 * valid JWT). Errors are thrown as [ApiException] → rendered by the Core GlobalExceptionHandler.
 */
@RestController
@RequestMapping("/v1")
class TenantAdminController(
    private val tenantService: TenantService,
    private val callerContext: CallerContext,
    private val objectMapper: ObjectMapper,
) {

    // ── Platform-admin: /v1/admin/tenants ───────────────────────────────────────────────────

    @PostMapping("/admin/tenants")
    fun create(@RequestBody(required = false) body: CreateTenantRequest?): ResponseEntity<Map<String, Any?>> {
        requireScope(SCOPE_PLATFORM_ADMIN)
        val tenant = tenantService.create(body ?: CreateTenantRequest())
        return ResponseEntity.status(HttpStatus.CREATED).body(toView(tenant))
    }

    @GetMapping("/admin/tenants")
    fun list(
        @RequestParam(required = false) cursor: String?,
        @RequestParam(required = false) limit: Int?,
        @RequestParam(name = "include_deleted", required = false, defaultValue = "false") includeDeleted: Boolean,
    ): Map<String, Any?> {
        requireScope(SCOPE_PLATFORM_ADMIN)
        val page = tenantService.list(cursor, limit, includeDeleted)
        return toPage(page)
    }

    @GetMapping("/admin/tenants/{tenantId}")
    fun get(@PathVariable tenantId: UUID): Map<String, Any?> {
        requireScope(SCOPE_PLATFORM_ADMIN)
        return toView(tenantService.get(tenantId))
    }

    @PatchMapping("/admin/tenants/{tenantId}/suspend")
    fun suspend(
        @PathVariable tenantId: UUID,
        @RequestBody(required = false) body: SuspendRequest?,
    ): Map<String, Any?> {
        requireScope(SCOPE_PLATFORM_ADMIN)
        return toView(tenantService.suspend(tenantId, body?.reason))
    }

    @PatchMapping("/admin/tenants/{tenantId}/resume")
    fun resume(@PathVariable tenantId: UUID): Map<String, Any?> {
        requireScope(SCOPE_PLATFORM_ADMIN)
        return toView(tenantService.resume(tenantId))
    }

    @DeleteMapping("/admin/tenants/{tenantId}")
    fun delete(@PathVariable tenantId: UUID): Map<String, Any?> {
        requireScope(SCOPE_PLATFORM_ADMIN)
        return toView(tenantService.softDelete(tenantId))
    }

    // ── Self-service: /v1/tenants/me ─────────────────────────────────────────────────────────

    @GetMapping("/tenants/me")
    fun getMe(): Map<String, Any?> {
        requireScope(SCOPE_TENANT_READ)
        val tenantId = callerContext.current().tenantId
        return toView(tenantService.get(tenantId))
    }

    @PatchMapping("/tenants/me")
    fun patchMe(@RequestBody(required = false) body: UpdateTenantRequest?): Map<String, Any?> {
        requireScope(SCOPE_TENANT_ADMIN)
        val tenantId = callerContext.current().tenantId
        return toView(tenantService.updateOwn(tenantId, body ?: UpdateTenantRequest()))
    }

    // ── Scope / identity helpers ─────────────────────────────────────────────────────────────

    /**
     * Programmatic scope guard, matching the rest of the auth-service (method security is not
     * enabled in the locked SecurityConfig, so `@PreAuthorize` would be silently ignored). The Core
     * [ai.cypherx.auth.web.AgentJwtAuthFilter] sets authorities as `SCOPE_<scope>`. A `platform:admin`
     * token satisfies any required scope. Missing scope -> Contract 2 403.
     */
    private fun requireScope(scope: String) {
        val authorities = SecurityContextHolder.getContext().authentication
            ?.authorities
            ?.map { it.authority }
            ?.toSet()
            ?: emptySet()
        if ("SCOPE_$scope" in authorities || "SCOPE_$SCOPE_PLATFORM_ADMIN" in authorities) return
        throw ApiException.forbidden(
            "Caller lacks required scope: $scope",
            mapOf("required" to scope),
        )
    }

    // ── View mapping ─────────────────────────────────────────────────────────────────────────

    private fun toView(t: Tenant): Map<String, Any?> = linkedMapOf(
        "tenant_id" to t.tenantId.toString(),
        "name" to t.name,
        "status" to t.status.value,
        "plan" to t.plan,
        "source" to t.source.value,
        "source_metadata" to parseJsonObject(t.sourceMetadataJson),
        "region" to t.region,
        "created_at" to TIMESTAMP_FMT.format(t.createdAt),
        "updated_at" to TIMESTAMP_FMT.format(t.updatedAt),
        "suspended_at" to t.suspendedAt?.let(TIMESTAMP_FMT::format),
        "pending_deletion_at" to t.pendingDeletionAt?.let(TIMESTAMP_FMT::format),
        "deleted_at" to t.deletedAt?.let(TIMESTAMP_FMT::format),
    )

    private fun toPage(page: TenantPage): Map<String, Any?> = linkedMapOf(
        "data" to page.items.map(::toView),
        "pagination" to linkedMapOf(
            "limit" to page.limit,
            "has_more" to page.hasMore,
            "next_cursor" to page.nextCursor,
            "total" to null,
        ),
    )

    /** Parse the stored JSONB `source_metadata` string back into an object for the response body. */
    private fun parseJsonObject(json: String): Map<String, Any?> =
        try {
            @Suppress("UNCHECKED_CAST")
            objectMapper.readValue(json, Map::class.java) as Map<String, Any?>
        } catch (ex: Exception) {
            emptyMap()
        }

    /** Inbound body for suspend (reason is optional). */
    data class SuspendRequest(val reason: String? = null)

    private companion object {
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
        const val SCOPE_TENANT_READ = "tenant:read"
        const val SCOPE_TENANT_ADMIN = "tenant:admin"
        val TIMESTAMP_FMT: DateTimeFormatter = DateTimeFormatter.ISO_INSTANT
    }
}
