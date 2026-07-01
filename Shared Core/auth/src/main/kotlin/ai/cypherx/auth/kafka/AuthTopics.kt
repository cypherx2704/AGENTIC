package ai.cypherx.auth.kafka

/**
 * Kafka event types published by the auth domain.
 *
 * These are NOT deployment tunables: Contract 5 §1 fixes the topic name to equal the
 * fully-qualified `event_type`, so each constant is a contract identifier (renaming one is a
 * cross-service breaking change), kept in code the same way the other services keep their
 * `TOPIC_*` constants.
 *
 * Durability classes (Phase 2 Amendment Log 2026-06):
 *  - DURABLE — written to `auth.outbox` in the SAME transaction as the state change and
 *    published by [OutboxRelay] (at-least-once; consumers de-duplicate on `event_id`):
 *    every `cypherx.tenant.*` lifecycle event, [TOKEN_REVOKED], [POLICY_CHANGED],
 *    [CONFIG_UPDATED].
 *  - ADVISORY — direct best-effort publish via [AuthEventPublisher] (log-and-drop on broker
 *    failure is acceptable; consumers self-heal via TTL'd caches): [AGENT_REGISTERED],
 *    [AGENT_UPDATED] and the 📋 post-first-cycle agent topics.
 */
object AuthTopics {

    // ── Advisory (direct best-effort; compact topics keyed by agent_id — topics.md §4.1) ──
    const val AGENT_REGISTERED = "cypherx.auth.agent.registered"
    const val AGENT_UPDATED = "cypherx.auth.agent.updated"

    /** 📋 post-first-cycle — reserved; NOT the topic for status updates (that is [AGENT_UPDATED]). */
    const val AGENT_DEACTIVATED = "cypherx.auth.agent.deactivated"

    // ── Durable (outbox-routed; keyed by tenant_id — Contract 5 §4) ────────────────────────
    const val POLICY_CHANGED = "cypherx.auth.policy.changed"
    const val CONFIG_UPDATED = "cypherx.auth.config.updated"
    const val TOKEN_REVOKED = "cypherx.auth.token.revoked"

    /**
     * Per-tenant effective-quota change (Component 1d / Contract 19). Emitted whenever an
     * `admin-override` (or other) row is appended to `auth.tenant_quotas`, so every quota consumer
     * (llms / guardrails / rag / memory / tools / skills / xagent) invalidates its cached effective
     * limits for the tenant. DURABLE: written to `auth.outbox` in the SAME transaction as the
     * `tenant_quotas` append (no log-and-drop — enforcement decisions depend on fresh limits).
     * A plan change is carried separately by [TENANT_PLAN_CHANGED] (which also invalidates quotas).
     */
    const val QUOTA_CHANGED = "cypherx.auth.quota.changed"
    const val TENANT_CREATED = "cypherx.tenant.created"
    const val TENANT_SUSPENDED = "cypherx.tenant.suspended"
    const val TENANT_RESUMED = "cypherx.tenant.resumed"
    const val TENANT_PLAN_CHANGED = "cypherx.tenant.plan_changed"

    /** Soft-delete (30-day grace start). Payload carries `grace_until` (Contract 13). */
    const val TENANT_PENDING_DELETION = "cypherx.tenant.pending_deletion"

    /** RESERVED for the hard-delete job (grace window elapsed) — soft-delete does NOT emit this. */
    const val TENANT_DELETED = "cypherx.tenant.deleted"
}
