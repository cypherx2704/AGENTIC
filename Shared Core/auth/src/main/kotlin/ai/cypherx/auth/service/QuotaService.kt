package ai.cypherx.auth.service

import ai.cypherx.auth.db.TenantTx
import ai.cypherx.auth.domain.TenantStatus
import ai.cypherx.auth.kafka.OutboxEventWriter
import ai.cypherx.auth.repo.PlanDefaultsRepository
import ai.cypherx.auth.repo.TenantQuotaRepository
import ai.cypherx.auth.repo.TenantQuotaRow
import ai.cypherx.auth.repo.TenantRepository
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import com.fasterxml.jackson.databind.node.ObjectNode
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.time.Instant
import java.util.UUID

/** `tenant_quotas.source` values (Contract 19). */
private const val SOURCE_PLAN_DEFAULT = "plan-default"
private const val SOURCE_ADMIN_OVERRIDE = "admin-override"
private const val SOURCE_BILLING_EVENT = "billing-event"

/** `change_type` carried by `cypherx.auth.quota.changed` (Component 1d). */
private const val CHANGE_OVERRIDE_SET = "override-set"

/**
 * Quota resolution + override management (Component 1d / Contract 19 — the per-tenant effective
 * limits document consumed by llms / guardrails / rag / memory / tools / skills / xagent).
 *
 * Resolution model:
 *  - `auth.plan_defaults[plan].limits` is the BASE document (per-service blocks).
 *  - The CURRENT `auth.tenant_quotas` row (`effective_until IS NULL`) is a PARTIAL override.
 *  - The effective document is a per-service, per-key DEEP MERGE: an override value replaces the
 *    plan default for that key; a key absent from the override inherits the plan default; nested
 *    objects merge recursively; scalars/arrays replace.
 *
 * Override writes are append-only and effective-dated (see [TenantQuotaRepository]). Setting an
 * override closes the previous current row, inserts the new current row, AND writes the
 * `cypherx.auth.quota.changed` outbox event — all in ONE tenant transaction (publication guarantee;
 * a quota consumer must never see a stale cache after the override commits). [OutboxEventWriter]
 * persists the Contract 5 envelope to `auth.outbox`; [ai.cypherx.auth.kafka.OutboxRelay] publishes
 * it asynchronously (at-least-once; consumers de-duplicate on `event_id`).
 *
 * Errors surface as [ApiException] so the Core GlobalExceptionHandler renders the Contract 2
 * envelope.
 */
@Service
class QuotaService(
    private val tenantRepository: TenantRepository,
    private val planDefaultsRepository: PlanDefaultsRepository,
    private val tenantQuotaRepository: TenantQuotaRepository,
    private val outboxEvents: OutboxEventWriter,
    private val tenantTx: TenantTx,
    private val objectMapper: ObjectMapper,
) {

    // ── Read / resolve ──────────────────────────────────────────────────────────────────────

    /**
     * Resolve the full effective limits document for [tenantId] (Contract 19 shape): the tenant's
     * plan defaults deep-merged with its current `tenant_quotas` override (override wins per-key).
     *
     * Throws 404 if the tenant does not exist, 422 if its plan has no `plan_defaults` row.
     */
    fun effectiveLimits(tenantId: UUID): JsonNode = resolve(tenantId).effective

    /**
     * Full resolution detail for admin reads: the tenant's plan, the plan-default base, the raw
     * current override (null when only plan defaults apply), and the merged effective document.
     */
    fun resolve(tenantId: UUID): QuotaResolution {
        val tenant = tenantRepository.findById(tenantId)
            ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))

        val planDefaults = planDefaultsRepository.limitsFor(tenant.plan)
            ?: throw ApiException.validation(
                "No plan_defaults for plan '${tenant.plan}'",
                mapOf("plan" to tenant.plan, "tenant_id" to tenantId.toString()),
            )

        val current: TenantQuotaRow? = tenantQuotaRepository.findCurrent(tenantId)

        // A `plan-default`-sourced row is the seed snapshot of the plan limits, NOT a true override
        // delta. Treating it as "no override" keeps plan changes flowing through `plan_defaults`
        // (the snapshot can reference an OLD plan's limits) and ensures an override is only ever a
        // sparse `admin-override` / `billing-event` delta merged on top of the live plan defaults.
        val overrideRow: TenantQuotaRow? = current?.takeIf { it.source != SOURCE_PLAN_DEFAULT }
        val overrideNode: JsonNode? = overrideRow?.let { parse(it.limitsJson) }

        val effective = if (overrideNode == null) {
            planDefaults.deepCopy()
        } else {
            deepMerge(planDefaults.deepCopy(), overrideNode)
        }

        return QuotaResolution(
            tenantId = tenantId,
            plan = tenant.plan,
            planDefaults = planDefaults,
            override = overrideNode,
            overrideSource = overrideRow?.source,
            overrideUpdatedBy = overrideRow?.updatedBy,
            overrideEffectiveFrom = overrideRow?.effectiveFrom,
            effective = effective,
        )
    }

    // ── Write (admin override) ───────────────────────────────────────────────────────────────

    /**
     * Set (or replace) a tenant's quota override (`source = 'admin-override'`). [limitsPatch] is a
     * partial limits document; it is deep-merged over the tenant's CURRENT effective override (or,
     * when none exists, over an empty object — so absent keys still inherit plan defaults at read
     * time). The merged override is written as a new current `tenant_quotas` row (closing the
     * previous one), and `cypherx.auth.quota.changed` is emitted in the SAME transaction.
     *
     * @return the freshly-resolved effective document (plan defaults deep-merged with the new override).
     */
    fun setOverride(tenantId: UUID, limitsPatch: JsonNode, updatedBy: String): QuotaResolution =
        applyOverride(tenantId, limitsPatch, SOURCE_ADMIN_OVERRIDE, updatedBy)

    /**
     * Set a tenant's quota override with a caller-chosen [source] (`admin-override` | `billing-event`).
     * Exposed so a billing adapter can record a billing-driven override on the same code path.
     */
    fun applyOverride(
        tenantId: UUID,
        limitsPatch: JsonNode,
        source: String,
        updatedBy: String,
    ): QuotaResolution {
        if (source != SOURCE_ADMIN_OVERRIDE && source != SOURCE_BILLING_EVENT && source != SOURCE_PLAN_DEFAULT) {
            throw ApiException.validation(
                "Invalid quota source",
                mapOf("source" to source, "allowed" to listOf(SOURCE_ADMIN_OVERRIDE, SOURCE_BILLING_EVENT, SOURCE_PLAN_DEFAULT)),
            )
        }
        if (!limitsPatch.isObject) {
            throw ApiException.validation("Quota patch must be a JSON object", mapOf("field" to "limits"))
        }

        val tenant = tenantRepository.findById(tenantId)
            ?: throw ApiException.notFound("Tenant not found", mapOf("tenant_id" to tenantId.toString()))
        if (tenant.status == TenantStatus.DELETED) {
            throw ApiException.conflict("Tenant is deleted", mapOf("tenant_id" to tenantId.toString()))
        }
        // Validate the plan has defaults before we persist (so reads can always resolve).
        planDefaultsRepository.limitsFor(tenant.plan)
            ?: throw ApiException.validation(
                "No plan_defaults for plan '${tenant.plan}'",
                mapOf("plan" to tenant.plan, "tenant_id" to tenantId.toString()),
            )

        // The new override = the patch deep-merged over the CURRENT override (or empty when none).
        // We persist only the OVERRIDE delta (not the full plan-merged doc) so a later plan change
        // still flows through plan_defaults for keys the tenant never overrode.
        val newOverrideJson = tenantTx.inTenant(tenantId) { jdbc ->
            val current = tenantQuotaRepository.readCurrent(jdbc, tenantId)
            // Carry forward only a true override delta; a `plan-default` seed row is NOT a delta, so
            // start from empty (the patch becomes the whole override; absent keys still inherit the
            // live plan defaults at read time).
            val baseOverride: ObjectNode = current
                ?.takeIf { it.source != SOURCE_PLAN_DEFAULT }
                ?.let { parseObject(it.limitsJson) }
                ?: objectMapper.createObjectNode()
            val mergedOverride = deepMerge(baseOverride, limitsPatch)
            val mergedOverrideJson = objectMapper.writeValueAsString(mergedOverride)

            val inserted = tenantQuotaRepository.appendOverrideInTx(
                jdbc = jdbc,
                tenantId = tenantId,
                plan = tenant.plan,
                limitsJson = mergedOverrideJson,
                source = source,
                updatedBy = updatedBy,
            )
            // Same-transaction outbox row: the override and its invalidation event commit together.
            outboxEvents.quotaChanged(
                jdbc,
                tenantId = tenantId,
                plan = tenant.plan,
                source = source,
                changeType = CHANGE_OVERRIDE_SET,
                effectiveAt = inserted.effectiveFrom,
            )
            mergedOverrideJson
        }

        log.info("quota override set for tenant {} (source={}, by={})", tenantId, source, updatedBy)

        // Re-resolve from the just-written override against plan defaults for the response.
        val planDefaults = planDefaultsRepository.limitsFor(tenant.plan)!!
        val overrideNode = parse(newOverrideJson)
        return QuotaResolution(
            tenantId = tenantId,
            plan = tenant.plan,
            planDefaults = planDefaults,
            override = overrideNode,
            overrideSource = source,
            overrideUpdatedBy = updatedBy,
            overrideEffectiveFrom = Instant.now(),
            effective = deepMerge(planDefaults.deepCopy(), overrideNode),
        )
    }

    // ── JSON helpers ─────────────────────────────────────────────────────────────────────────

    /**
     * Deep-merge [patch] INTO [target] (mutating and returning [target]). Per Contract 19 semantics:
     *  - both nodes are objects for a key -> recurse;
     *  - otherwise the patch value replaces the target value (scalars and arrays replace wholesale);
     *  - a key present only in target is kept; a key present only in patch is added.
     *
     * Callers pass a `deepCopy()` of any shared base node they do not want mutated.
     */
    private fun deepMerge(target: JsonNode, patch: JsonNode): JsonNode {
        if (target !is ObjectNode || patch !is ObjectNode) return patch
        patch.fields().forEach { (key, patchValue) ->
            val targetValue = target.get(key)
            if (targetValue != null && targetValue.isObject && patchValue.isObject) {
                target.set<JsonNode>(key, deepMerge(targetValue, patchValue))
            } else {
                target.set<JsonNode>(key, patchValue.deepCopy())
            }
        }
        return target
    }

    private fun parse(json: String): JsonNode =
        try {
            objectMapper.readTree(json) ?: objectMapper.createObjectNode()
        } catch (ex: Exception) {
            throw ApiException.internal("Stored quota limits are not valid JSON")
        }

    private fun parseObject(json: String): ObjectNode {
        val node = parse(json)
        return node as? ObjectNode ?: objectMapper.createObjectNode()
    }

    private companion object {
        val log = LoggerFactory.getLogger(QuotaService::class.java)
    }
}

/**
 * The full resolution of a tenant's quotas: the plan, the plan-default base, the raw override (null
 * when only plan defaults apply) plus its provenance, and the merged effective document.
 */
data class QuotaResolution(
    val tenantId: UUID,
    val plan: String,
    val planDefaults: JsonNode,
    val override: JsonNode?,
    val overrideSource: String?,
    val overrideUpdatedBy: String?,
    val overrideEffectiveFrom: Instant?,
    val effective: JsonNode,
)
