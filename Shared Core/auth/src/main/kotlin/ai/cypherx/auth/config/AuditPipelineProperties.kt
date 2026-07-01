package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.audit-pipeline.*` configuration tree (WP04 — the
 * audit export/mirror pipeline + `/v1/usage` rollup). Bound automatically by
 * @ConfigurationPropertiesScan on [ai.cypherx.auth.AuthApplication].
 *
 * Every value is env-overridable (e.g. `AUDIT_EXPORT_STORE`, `AWS_S3_BUCKET`); the in-code defaults
 * are the documented last-resort fallbacks — nothing here is a hardcoded tunable.
 *
 * FAIL-SOFT contract (Phase 2 / WP04): the pluggable [ObjectStore] and the two Kafka consumers must
 * boot and no-op cleanly when their backend env is unset:
 *  - [store] == `local` (default) writes to the local filesystem ([Local.basePath]) — no AWS needed;
 *  - the Kafka consumers are OFF by default ([AuditMirror.enabled] / [UsageRollup.enabled] = false)
 *    so a broker-less local boot or the test profile (which excludes KafkaAutoConfiguration) starts
 *    with no listener container at all. Enable them only where a broker is configured.
 */
@ConfigurationProperties(prefix = "cypherx.auth.audit-pipeline")
data class AuditPipelineProperties(

    /**
     * Object-store backend selector (env `AUDIT_EXPORT_STORE`): `local` | `s3` | `minio`.
     * `local` (default) is the dev/test filesystem store — no AWS credentials required.
     * `s3` / `minio` use SigV4-signed HTTP (no AWS SDK dependency) against [s3].
     */
    val store: String = "local",

    /** Export presigned-URL TTL. Contract default = 7 days. */
    val exportUrlTtlSeconds: Long = 604_800,

    /**
     * Max audit rows streamed into a single export object (guards an unbounded scan / object size).
     * Beyond this, the export truncates and records `truncated=true` on the job row.
     */
    val exportMaxRows: Long = 1_000_000,

    /** Object-key prefix for on-demand exports written by the export endpoint. */
    val exportKeyPrefix: String = "audit-exports",

    /** Object-key prefix for the continuous mirror written by [AuditMirror]. */
    val mirrorKeyPrefix: String = "audit-mirror",

    /** Local-filesystem store knobs (used when [store] == `local`). */
    val local: Local = Local(),

    /** S3 / MinIO store knobs (used when [store] == `s3` or `minio`). */
    val s3: S3 = S3(),

    /** Audit-mirror Kafka consumer knobs. */
    val auditMirror: AuditMirror = AuditMirror(),

    /** Usage-rollup Kafka consumer knobs. */
    val usageRollup: UsageRollup = UsageRollup(),

    /** Hourly per-tenant audit chain-verification job knobs. */
    val chainVerify: ChainVerify = ChainVerify(),
) {

    /**
     * Local filesystem object store (dev/test). Presigned URLs are `file://` URIs (no signing) —
     * the export is readable directly from the mounted volume; a 7d "TTL" is advisory only here.
     */
    data class Local(
        /** Root directory exports/mirror objects are written under (env `AUDIT_EXPORT_LOCAL_DIR`). */
        val basePath: String = "/var/lib/cypherx/auth/audit-export",
    )

    /**
     * S3 / MinIO object store. Credentials and endpoint come from env (12-factor): standard
     * AWS env vars back [accessKeyId]/[secretAccessKey]/[region]; [endpoint] is set for MinIO
     * (path-style) and left null for real AWS S3 (virtual-hosted style).
     *
     * Presigned URLs are AWS SigV4 query-string-signed GET URLs (no AWS SDK required — see
     * [ai.cypherx.auth.service.s3.S3ObjectStore]). Empty/blank [bucket] OR missing creds => the S3
     * store is unconfigured and fails soft (the factory falls back to a no-op store; exports return
     * SERVICE_UNAVAILABLE rather than crashing the app).
     */
    data class S3(
        /** Target bucket (env `AWS_S3_BUCKET`). Blank => S3 store unconfigured (fail-soft). */
        val bucket: String = "",

        /** AWS region (env `AWS_REGION`). */
        val region: String = "us-east-1",

        /** Access key id (env `AWS_ACCESS_KEY_ID`). Blank => unconfigured. */
        val accessKeyId: String = "",

        /** Secret access key (env `AWS_SECRET_ACCESS_KEY`). Blank => unconfigured. */
        val secretAccessKey: String = "",

        /** Optional STS session token (env `AWS_SESSION_TOKEN`) when using temporary creds. */
        val sessionToken: String? = null,

        /**
         * Custom endpoint for MinIO / S3-compatible stores (env `AWS_S3_ENDPOINT`, e.g.
         * `http://minio:9000`). Null/blank => real AWS S3 (virtual-hosted-style host derived from
         * bucket + region). When set, path-style addressing (`{endpoint}/{bucket}/{key}`) is used.
         */
        val endpoint: String? = null,

        /** Per-request HTTP timeout (ms) for PUT/HEAD against the object store. */
        val httpTimeoutMs: Long = 10_000,
    )

    /**
     * Audit-mirror Kafka consumer (mirrors the durable audit-append topic to object storage). OFF by
     * default so a broker-less boot / the test profile starts clean. The topic is
     * [topic] (default `cypherx.auth.audit.appended`).
     */
    data class AuditMirror(
        /** Master switch. Default OFF (fail-soft on broker-less boots / tests). */
        val enabled: Boolean = false,

        /**
         * Durable audit-append topic mirrored to object storage. Auth does not yet emit a dedicated
         * audit-append event in WP04, so this defaults to the RESERVED topic name the mirror will
         * consume once Auth publishes it; documented so the contract is fixed.
         */
        val topic: String = "cypherx.auth.audit.appended",

        /** Consumer group id. */
        val groupId: String = "auth-audit-mirror",
    )

    /**
     * Usage-rollup Kafka consumer (Component 1d / Contract 19 — `cypherx.llms.usage.recorded` →
     * `auth.tenant_usage_counters`). OFF by default so a broker-less boot / the test profile starts
     * clean.
     */
    data class UsageRollup(
        /** Master switch. Default OFF (fail-soft on broker-less boots / tests). */
        val enabled: Boolean = false,

        /** Contract 19 usage topic consumed and rolled into the per-tenant hourly counters. */
        val topic: String = "cypherx.llms.usage.recorded",

        /** Consumer group id. */
        val groupId: String = "auth-usage-rollup",
    )

    /**
     * Hourly per-tenant audit chain-verification job ([ai.cypherx.auth.service.AuditChainVerifyJob]).
     * Re-walks each tenant's tamper-evident hash chain; logs + emits a metric on any break.
     */
    data class ChainVerify(
        /** Master switch for the scheduled job. */
        val enabled: Boolean = true,

        /** Fixed-delay cadence (ms). Default hourly. */
        val sweepMs: Long = 3_600_000,

        /**
         * Only verify the trailing window (hours) of each tenant's chain per sweep, so the hourly
         * job stays bounded on large logs. The full chain is still anchored on the window's first
         * stored prev-hash (see [ai.cypherx.auth.service.AuditService.verifyChain]).
         */
        val windowHours: Long = 24,

        /** Max tenants verified per sweep (bounds one pass; the rest roll to the next sweep). */
        val maxTenantsPerSweep: Int = 5_000,
    )
}
