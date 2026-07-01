package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.INTEGRATION_TEST_TENANT_ID
import ai.cypherx.auth.domain.SYSTEM_USER_ID
import ai.cypherx.auth.domain.TenantSource
import ai.cypherx.auth.domain.TenantStatus
import ai.cypherx.auth.kafka.OutboxEventWriter
import ai.cypherx.auth.repo.Tenant
import ai.cypherx.auth.repo.TenantRepository
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.LoggerFactory
import org.springframework.dao.DuplicateKeyException
import org.springframework.stereotype.Service
import java.time.Duration
import java.time.Instant
import java.util.Base64
import java.util.UUID

/** Bounds for tenant list pagination (Contract 9). */
private const val DEFAULT_PAGE_LIMIT = 50
private const val MAX_PAGE_LIMIT = 200

/**
 * Tenant lifecycle orchestration (Phase 2 Component 1b). Owns:
 *  - create (caller- or auto-supplied id; seed `tenant_quotas` from `plan_defaults`),
 *  - cursor-paginated list + get,
 *  - suspend / resume / plan-change / soft-delete,
 *  - emitting the Contract 13 `cypherx.tenant.*` lifecycle events for every transition.
 *
 * Publication guarantee (amended 2026-06): every lifecycle event is written to the
 * transactional outbox (`auth.outbox`, [OutboxEventWriter]) in the SAME transaction as the
 * tenant state change — the mutation and its event commit or roll back together, and the
 * [ai.cypherx.auth.kafka.OutboxRelay] publishes asynchronously. Best-effort log-and-drop is
 * NOT permitted for the provisioning backbone (≤5s staleness SLA, Audit Addendum #6). The
 * repository methods participate in the wrapping [TenantTx.inPlatform] transaction
 * (TransactionTemplate propagation REQUIRED), so no second transaction is opened.
 *
 * Errors surface as [ApiException] so the Core GlobalExceptionHandler renders the Contract 2
 * envelope.
 */
@Service
class TenantService(
    private val tenantRepository: TenantRepository,
    private val outboxEvents: OutboxEventWriter,
    private val tenantTx: TenantTx,
    private val objectMapper: ObjectMapper,
    private val props: AuthProperties,
) {

    // ── Create ────────────────────────────────────────────────────────────────────────────

    /**
     * Create a tenant. Admin-initiated creates default to the `manual-seed` / `external-admin`
     * sources (Contract 13). Seeds the initial `tenant_quotas` row from `plan_defaults[plan]` and
     * emits `cypherx.tenant.created`.
     */
    fun create(req: CreateTenantRequest): Tenant {
        val name = req.name?.trim().orEmpty()
        if (name.isEmpty()) {
            throw ApiException.validation("Tenant name is required", mapOf("field" to "name"))
        }
        val plan = req.plan?.trim().orEmpty().ifEmpty { "free" }
        val region = req.region?.trim().orEmpty().ifEmpty { "us-east-1" }

        val source = parseSource(req.source) ?: TenantSource.MANUAL_SEED
        // Admin API is the manual / external-admin path; reject sources that only the px0 bridge
        // or self-serve/SSO funnels legitimately produce, so audit provenance stays honest.
        if (source != TenantSource.MANUAL_SEED && source != TenantSource.EXTERNAL_ADMIN) {
            throw ApiException.validation(
                "source must be manual-seed or external-admin for admin-created tenants",
                mapOf("field" to "source", "allowed" to listOf("manual-seed", "external-admin")),
            )
        }

        val tenantId = parseOptionalUuid(req.tenantId, "tenant_id") ?: UUID.randomUUID()
        // The CI integration-test tenant must never be created in prod.
        if (tenantId == INTEGRATION_TEST_TENANT_ID && props.environment.equals("prod", ignoreCase = true)) {
            throw ApiException.forbidden("The integration-test tenant is not permitted in prod")
        }

        // Validate the plan exists before we insert; we also reuse its limits to seed quotas.
        val limitsJson = tenantRepository.planDefaultLimits(plan)
            ?: throw ApiException.validation(
                "Unknown plan '$plan'",
                mapOf("field" to "plan", "plan" to plan),
            )

        val sourceMetadataJson = writeJson(req.sourceMetadata)

        // Insert + `cypherx.tenant.created` outbox row in ONE transaction (publication guarantee).
        val tenant = try {
            tenantTx.inPlatform { jdbc ->
                val created = tenantRepository.insert(tenantId, name, plan, source, sourceMetadataJson, region)
                outboxEvents.tenantCreated(
                    jdbc,
                    tenantId = tenantId,
                    plan = plan,
                    source = source.value,
                    region = region,
                    createdAt = created.createdAt,
                )
                created
            }
        } catch (ex: DuplicateKeyException) {
            throw ApiException.conflict(
                "Tenant already exists",
                mapOf("tenant_id" to tenantId.toString()),
            )
        }

        // Seed the initial effective-quota row from the plan defaults (Contract 19). Deliberately a
        // SEPARATE (best-effort) transaction so an unlikely seeding failure cannot poison the
        // already-committed create — the quota row is re-derivable from plan_defaults.
        runCatching {
            tenantRepository.seedQuotasFromPlan(tenantId, plan, limitsJson, updatedBy = SYSTEM_USER_ID.toString())
        }.onFailure { log.warn("failed to seed quotas for tenant {}: {}", tenantId, it.message) }

        return tenant
    }

    // ── Read ──────────────────────────────────────────────────────────────────────────────

    /** Fetch a tenant by id, or 404 if it does not exist. */
    fun get(tenantId: UUID): Tenant =
        tenantRepository.findById(tenantId)
            ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))

    /**
     * Cursor-paginated list (Contract 9). [cursor] is the opaque token returned as `next_cursor` on
     * the previous page. Over-fetches by one to compute `has_more`.
     */
    fun list(cursor: String?, requestedLimit: Int?, includeDeleted: Boolean): TenantPage {
        val limit = (requestedLimit ?: DEFAULT_PAGE_LIMIT).coerceIn(1, MAX_PAGE_LIMIT)
        val decoded = cursor?.let { decodeCursor(it) }

        val rows = tenantRepository.list(
            limit = limit + 1,
            afterCreatedAt = decoded?.first,
            afterTenantId = decoded?.second,
            includeDeleted = includeDeleted,
        )
        val hasMore = rows.size > limit
        val page = if (hasMore) rows.subList(0, limit) else rows
        val nextCursor = if (hasMore) {
            page.lastOrNull()?.let { encodeCursor(it.createdAt, it.tenantId) }
        } else {
            null
        }
        return TenantPage(items = page, limit = limit, hasMore = hasMore, nextCursor = nextCursor)
    }

    // ── Update (self-service /v1/tenants/me) ───────────────────────────────────────────────

    /**
     * Apply caller-editable fields to the caller's own tenant. Plan changes are NOT permitted here
     * (those arrive via billing → [changePlan]); only `name`, `region`, and `source_metadata` may be
     * patched. Returns the refreshed row; 404 if the tenant is gone/deleted.
     */
    fun updateOwn(tenantId: UUID, req: UpdateTenantRequest): Tenant {
        val name = req.name?.trim()?.takeIf { it.isNotEmpty() }
        val region = req.region?.trim()?.takeIf { it.isNotEmpty() }
        val metadataJson = req.sourceMetadata?.let { writeJson(it) }
        if (name == null && region == null && metadataJson == null) {
            throw ApiException.validation("No updatable fields provided")
        }
        return tenantRepository.updateMutable(tenantId, name, region, metadataJson)
            ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))
    }

    // ── Lifecycle transitions ──────────────────────────────────────────────────────────────

    /** Suspend a tenant and emit `cypherx.tenant.suspended`. No-op-safe if already suspended. */
    fun suspend(tenantId: UUID, reason: String?): Tenant {
        val current = get(tenantId)
        if (current.status == TenantStatus.DELETED || current.status == TenantStatus.PENDING_DELETION) {
            throw ApiException.conflict(
                "Cannot suspend a tenant pending deletion",
                mapOf("tenant_id" to tenantId.toString(), "status" to current.status.value),
            )
        }
        // Status flip + `cypherx.tenant.suspended` outbox row in ONE transaction.
        return tenantTx.inPlatform { jdbc ->
            val tenant = tenantRepository.suspend(tenantId)
                ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))
            outboxEvents.tenantSuspended(
                jdbc,
                tenantId = tenantId,
                reason = reason?.trim()?.takeIf { it.isNotEmpty() } ?: "admin-action",
                suspendedAt = tenant.suspendedAt ?: Instant.now(),
            )
            tenant
        }
    }

    /** Resume a suspended tenant and emit `cypherx.tenant.resumed`. */
    fun resume(tenantId: UUID): Tenant {
        val current = get(tenantId)
        if (current.status == TenantStatus.DELETED || current.status == TenantStatus.PENDING_DELETION) {
            throw ApiException.conflict(
                "Cannot resume a tenant pending deletion",
                mapOf("tenant_id" to tenantId.toString(), "status" to current.status.value),
            )
        }
        // Status flip + `cypherx.tenant.resumed` outbox row in ONE transaction.
        return tenantTx.inPlatform { jdbc ->
            val tenant = tenantRepository.resume(tenantId)
                ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))
            outboxEvents.tenantResumed(jdbc, tenantId = tenantId, resumedAt = tenant.updatedAt)
            tenant
        }
    }

    /**
     * Change a tenant's plan and emit `cypherx.tenant.plan_changed`. Validates the new plan exists.
     * Exposed for billing-driven and admin-driven plan transitions (the px0/Stripe adapters call
     * this when a `cypherx.tenant.plan_changed` upstream arrives).
     */
    fun changePlan(tenantId: UUID, newPlan: String, source: String?): Tenant {
        val target = newPlan.trim()
        if (target.isEmpty()) {
            throw ApiException.validation("new_plan is required", mapOf("field" to "new_plan"))
        }
        val current = get(tenantId)
        if (current.status == TenantStatus.DELETED) {
            throw ApiException.conflict("Tenant is deleted", mapOf("tenant_id" to tenantId.toString()))
        }
        tenantRepository.planDefaultLimits(target)
            ?: throw ApiException.validation("Unknown plan '$target'", mapOf("field" to "new_plan", "plan" to target))
        val oldPlan = current.plan
        if (oldPlan == target) {
            return current
        }
        // Plan flip + `cypherx.tenant.plan_changed` outbox row in ONE transaction.
        return tenantTx.inPlatform { jdbc ->
            val tenant = tenantRepository.updatePlan(tenantId, target)
                ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))
            outboxEvents.tenantPlanChanged(
                jdbc,
                tenantId = tenantId,
                oldPlan = oldPlan,
                newPlan = target,
                effectiveAt = tenant.updatedAt,
            )
            tenant
        }
    }

    /**
     * Soft-delete a tenant: move to `pending_deletion` (grace window from
     * `cypherx.auth.tenant-deletion-grace-days`, default 30 — Contract 13). The durable row is
     * retained; a later hard-delete job flips it to `deleted` once the grace window elapses.
     *
     * Event fidelity (amended 2026-06): soft-delete emits `cypherx.tenant.pending_deletion`
     * (payload: tenant_id, grace_until) so downstream consumers start their erasure countdown.
     * `cypherx.tenant.deleted` is RESERVED for the hard-delete job once it lands.
     */
    fun softDelete(tenantId: UUID): Tenant {
        val current = get(tenantId)
        if (current.status == TenantStatus.DELETED) {
            throw ApiException.conflict("Tenant already deleted", mapOf("tenant_id" to tenantId.toString()))
        }
        // Status flip + `cypherx.tenant.pending_deletion` outbox row in ONE transaction.
        return tenantTx.inPlatform { jdbc ->
            val tenant = tenantRepository.softDelete(tenantId)
                ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))
            val graceStart = tenant.pendingDeletionAt ?: Instant.now()
            outboxEvents.tenantPendingDeletion(
                jdbc,
                tenantId = tenantId,
                graceUntil = graceStart.plus(Duration.ofDays(props.tenantDeletionGraceDays)),
            )
            tenant
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────────────────

    private fun parseSource(raw: String?): TenantSource? {
        val v = raw?.trim().orEmpty()
        if (v.isEmpty()) return null
        return try {
            TenantSource.from(v)
        } catch (ex: IllegalArgumentException) {
            throw ApiException.validation(
                "Unknown source '$v'",
                mapOf("field" to "source", "value" to v),
            )
        }
    }

    private fun parseOptionalUuid(raw: String?, field: String): UUID? {
        val v = raw?.trim().orEmpty()
        if (v.isEmpty()) return null
        return try {
            UUID.fromString(v)
        } catch (ex: IllegalArgumentException) {
            throw ApiException.validation("Invalid $field (must be a UUID)", mapOf("field" to field, "value" to v))
        }
    }

    private fun writeJson(map: Map<String, Any?>?): String =
        if (map.isNullOrEmpty()) "{}" else objectMapper.writeValueAsString(map)

    private fun encodeCursor(createdAt: Instant, tenantId: UUID): String {
        val raw = "${createdAt.toEpochMilli()}:$tenantId"
        return Base64.getUrlEncoder().withoutPadding().encodeToString(raw.toByteArray(Charsets.UTF_8))
    }

    private fun decodeCursor(cursor: String): Pair<Instant, UUID> {
        return try {
            val raw = String(Base64.getUrlDecoder().decode(cursor), Charsets.UTF_8)
            val sep = raw.indexOf(':')
            require(sep > 0)
            val millis = raw.substring(0, sep).toLong()
            val id = UUID.fromString(raw.substring(sep + 1))
            Instant.ofEpochMilli(millis) to id
        } catch (ex: Exception) {
            throw ApiException.validation("Invalid cursor", mapOf("field" to "cursor"))
        }
    }

    private companion object {
        val log = LoggerFactory.getLogger(TenantService::class.java)
    }
}

// ── Service-level DTOs (request inputs + list page) ─────────────────────────────────────────

/** Inbound payload for `POST /v1/admin/tenants`. */
data class CreateTenantRequest(
    val tenantId: String? = null,
    val name: String? = null,
    val plan: String? = null,
    val source: String? = null,
    val region: String? = null,
    val sourceMetadata: Map<String, Any?>? = null,
)

/** Inbound payload for `PATCH /v1/tenants/me`. */
data class UpdateTenantRequest(
    val name: String? = null,
    val region: String? = null,
    val sourceMetadata: Map<String, Any?>? = null,
)

/** A page of tenants plus the pagination metadata the controller maps onto the Contract 9 envelope. */
data class TenantPage(
    val items: List<Tenant>,
    val limit: Int,
    val hasMore: Boolean,
    val nextCursor: String?,
)
