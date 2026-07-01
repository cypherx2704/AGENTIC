package ai.cypherx.auth.kafka

import ai.cypherx.auth.repo.OutboxRepository
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.stereotype.Component
import java.time.Instant
import java.util.UUID

/**
 * Writes DURABLE auth events to the transactional outbox (Phase 2 Amendment Log 2026-06).
 *
 * Every method takes the CALLER's open transactional [JdbcTemplate] (handed out by a
 * [ai.cypherx.auth.db.TenantTx] block) so the Contract 5 envelope row commits atomically WITH
 * the state change it describes. If the state change rolls back, so does the event — and vice
 * versa. [OutboxRelay] publishes the rows to Kafka asynchronously (at-least-once; consumers
 * de-duplicate on `event_id`).
 *
 * Covered topics (no log-and-drop allowed for any of these — ≤5s staleness SLA):
 *  - `cypherx.tenant.*` lifecycle (Contract 13 provisioning backbone),
 *  - `cypherx.auth.token.revoked` (Component 3c),
 *  - `cypherx.auth.policy.changed` (/authorize cache invalidation),
 *  - `cypherx.auth.config.updated` (Component 4 rate-limit/config hot-reload).
 *
 * Advisory topics (agent.registered / agent.updated) stay on the direct best-effort path —
 * see [AuthEventPublisher].
 *
 * Serialization failures propagate (rolling back the surrounding transaction): a durable
 * event that cannot be recorded must abort the state change, never silently vanish.
 */
@Component
class OutboxEventWriter(
    private val outboxRepository: OutboxRepository,
    private val envelopes: EventEnvelopeFactory,
) {

    // ── Tenant lifecycle (Contract 13 — keyed by tenant_id) ──────────────────────────────

    /** cypherx.tenant.created — payload per contracts/kafka/events/tenant.created.schema.json. */
    fun tenantCreated(jdbc: JdbcTemplate, tenantId: UUID, plan: String, source: String, region: String?, createdAt: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.TENANT_CREATED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "source" to source,
                "plan" to plan,
                "region" to region,
                "created_at" to envelopes.iso(createdAt),
            ),
        )
    }

    /** cypherx.tenant.suspended — payload per contracts/kafka/events/tenant.suspended.schema.json. */
    fun tenantSuspended(jdbc: JdbcTemplate, tenantId: UUID, reason: String, suspendedAt: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.TENANT_SUSPENDED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "reason" to reason,
                "suspended_at" to envelopes.iso(suspendedAt),
            ),
        )
    }

    /** cypherx.tenant.resumed. */
    fun tenantResumed(jdbc: JdbcTemplate, tenantId: UUID, resumedAt: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.TENANT_RESUMED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "resumed_at" to envelopes.iso(resumedAt),
            ),
        )
    }

    /** cypherx.tenant.plan_changed — also drives per-tenant quota cache invalidation (Component 1d). */
    fun tenantPlanChanged(jdbc: JdbcTemplate, tenantId: UUID, oldPlan: String?, newPlan: String, effectiveAt: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.TENANT_PLAN_CHANGED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "old_plan" to oldPlan,
                "new_plan" to newPlan,
                "effective_at" to envelopes.iso(effectiveAt),
            ),
        )
    }

    /**
     * cypherx.tenant.pending_deletion — soft-delete (grace window starts). `grace_until` is when
     * downstream consumers may hard-erase (Contract 13). `cypherx.tenant.deleted` is RESERVED for
     * the hard-delete job — see [tenantDeleted].
     */
    fun tenantPendingDeletion(jdbc: JdbcTemplate, tenantId: UUID, graceUntil: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.TENANT_PENDING_DELETION,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "grace_until" to envelopes.iso(graceUntil),
            ),
        )
    }

    /**
     * cypherx.tenant.deleted — HARD delete only (the 30-day grace window elapsed). No first-cycle
     * caller: the hard-delete job lands in a later phase; soft-delete emits [tenantPendingDeletion].
     */
    fun tenantDeleted(jdbc: JdbcTemplate, tenantId: UUID, deletedAt: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.TENANT_DELETED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "deleted_at" to envelopes.iso(deletedAt),
            ),
        )
    }

    // ── Token revocation (Component 3c) ──────────────────────────────────────────────────

    /**
     * cypherx.auth.token.revoked — every verifier subscribes and primes its bloom filter / Valkey
     * deny-set. Keyed by tenant_id (per-tenant ordering); agent_id is carried in the payload.
     */
    fun tokenRevoked(
        jdbc: JdbcTemplate,
        jti: UUID,
        tenantId: UUID,
        agentId: UUID?,
        reason: String,
        tokenExp: Instant,
        revokedAt: Instant,
    ) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.TOKEN_REVOKED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "jti" to jti.toString(),
                "agent_id" to agentId?.toString(),
                "tenant_id" to tenantId.toString(),
                "token_exp" to envelopes.iso(tokenExp),
                "reason" to reason,
                "revoked_at" to envelopes.iso(revokedAt),
            ),
        )
    }

    // ── Policy / config invalidation (Component 4) ───────────────────────────────────────

    /** cypherx.auth.policy.changed — invalidate authz caches for [tenantId]. */
    fun policyChanged(jdbc: JdbcTemplate, tenantId: UUID, policyId: UUID?, changeType: String, updatedAt: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.POLICY_CHANGED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "policy_id" to policyId?.toString(),
                "tenant_id" to tenantId.toString(),
                "change_type" to changeType,
                "updated_at" to envelopes.iso(updatedAt),
            ),
        )
    }

    /**
     * cypherx.auth.config.updated — hot-reload trigger for `auth.rate_limit_config` and future
     * runtime config tables (Component 4). `configKind` names the table/config family that changed.
     */
    fun configUpdated(jdbc: JdbcTemplate, tenantId: UUID, configKind: String, changeType: String, updatedAt: Instant) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.CONFIG_UPDATED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "config_kind" to configKind,
                "change_type" to changeType,
                "updated_at" to envelopes.iso(updatedAt),
            ),
        )
    }

    // ── Quota invalidation (Component 1d / Contract 19) ──────────────────────────────────

    /**
     * cypherx.auth.quota.changed — a tenant's effective quota override changed; consumers
     * (llms / guardrails / rag / memory / tools / skills / xagent) must invalidate their cached
     * effective limits for [tenantId]. Keyed by tenant_id. [source] is the `tenant_quotas.source`
     * that produced the new current row (`admin-override` | `billing-event` | `plan-default`);
     * [changeType] names what happened (e.g. `override-set`).
     */
    fun quotaChanged(
        jdbc: JdbcTemplate,
        tenantId: UUID,
        plan: String,
        source: String,
        changeType: String,
        effectiveAt: Instant,
    ) {
        writeTenantKeyed(
            jdbc,
            eventType = AuthTopics.QUOTA_CHANGED,
            tenantId = tenantId,
            payload = linkedMapOf(
                "tenant_id" to tenantId.toString(),
                "plan" to plan,
                "source" to source,
                "change_type" to changeType,
                "effective_at" to envelopes.iso(effectiveAt),
            ),
        )
    }

    // ── Internal ─────────────────────────────────────────────────────────────────────────

    /** Envelope + insert, keyed by tenant_id (Contract 5 §4 default for tenant-scoped events). */
    private fun writeTenantKeyed(jdbc: JdbcTemplate, eventType: String, tenantId: UUID, payload: Map<String, Any?>) {
        val key = tenantId.toString()
        outboxRepository.insertInTx(jdbc, eventType, key, envelopes.json(eventType, tenantId, key, payload))
    }
}
