package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuditPipelineProperties
import ai.cypherx.auth.repo.TenantRepository
import io.micrometer.core.instrument.MeterRegistry
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.ObjectProvider
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.UUID

/**
 * Hourly per-tenant audit chain-verification job (Component 6 — WP04).
 *
 * Every sweep walks each tenant's tamper-evident hash chain over a trailing window
 * ([AuditPipelineProperties.ChainVerify.windowHours]) via the existing
 * [AuditService.verifyChain]. On any break it logs at ERROR and increments the metric
 * `auth_audit_chain_broken_total{tenant}` so alerting fires; a clean sweep increments
 * `auth_audit_chain_verified_total`. The job NEVER mutates the log (the runtime role has no
 * UPDATE/DELETE on `audit_log`); detection + alert is the whole job.
 *
 * Tenants are enumerated from `auth.tenants` (platform-scoped) in cursor-paginated pages, bounded by
 * [AuditPipelineProperties.ChainVerify.maxTenantsPerSweep] so one pass stays bounded on a large
 * fleet; soft-deleted tenants are skipped (their chains are frozen). A per-tenant verify failure
 * (e.g. transient DB error) is logged and the sweep continues — one tenant must not stall the rest.
 *
 * Enabled by default ([AuditPipelineProperties.ChainVerify.enabled]); disable with
 * `cypherx.auth.audit-pipeline.chain-verify.enabled=false`. It depends only on the DB (no Kafka /
 * object store), so it runs safely on any boot, including local. The test profile leaves it enabled
 * but it is harmless there (it verifies whatever rows exist).
 */
@Component
@ConditionalOnProperty(
    prefix = "cypherx.auth.audit-pipeline.chain-verify",
    name = ["enabled"],
    havingValue = "true",
    matchIfMissing = true,
)
class AuditChainVerifyJob(
    private val auditService: AuditService,
    private val tenantRepository: TenantRepository,
    private val props: AuditPipelineProperties,
    meterRegistryProvider: ObjectProvider<MeterRegistry>,
) {

    /** null when no metrics registry is wired — the job then logs only (still verifies). */
    private val meterRegistry: MeterRegistry? = meterRegistryProvider.ifAvailable

    /** Scheduled entrypoint. Cadence = [AuditPipelineProperties.ChainVerify.sweepMs] (default hourly). */
    @Scheduled(fixedDelayString = "\${cypherx.auth.audit-pipeline.chain-verify.sweep-ms:3600000}")
    fun sweep() {
        val cfg = props.chainVerify
        val from = Instant.now().minus(cfg.windowHours, ChronoUnit.HOURS)
        var verified = 0
        var broken = 0
        var errored = 0

        for (tenantId in enumerateTenants(cfg.maxTenantsPerSweep)) {
            try {
                val result = auditService.verifyChain(tenantId, from, null)
                if (result.ok) {
                    verified++
                    counter("auth_audit_chain_verified_total")
                } else {
                    broken++
                    counter("auth_audit_chain_broken_total", "tenant_id", tenantId.toString())
                    log.error(
                        "AUDIT CHAIN BROKEN tenant={} broken_at_row_id={} expected_prev={} actual={}",
                        tenantId,
                        result.brokenAtRowId,
                        result.expectedPrevHashHex,
                        result.actualPrevHashHex,
                    )
                }
            } catch (ex: Exception) {
                errored++
                log.warn("audit chain verify failed for tenant {} (continuing): {}", tenantId, ex.message)
            }
        }

        if (broken > 0) {
            log.error("audit chain verify sweep complete: verified={} BROKEN={} errored={}", verified, broken, errored)
        } else {
            log.info("audit chain verify sweep complete: verified={} broken=0 errored={}", verified, errored)
        }
    }

    /**
     * Enumerate non-deleted tenant ids in keyset-paginated pages (using [TenantRepository.list], which
     * already hides `deleted` tenants), capped at [max]. The list is platform-scoped (no RLS).
     */
    private fun enumerateTenants(max: Int): List<UUID> {
        val ids = mutableListOf<UUID>()
        var afterCreatedAt: Instant? = null
        var afterTenantId: UUID? = null
        while (ids.size < max) {
            val remaining = (max - ids.size).coerceAtMost(PAGE_SIZE)
            val page = tenantRepository.list(
                limit = remaining,
                afterCreatedAt = afterCreatedAt,
                afterTenantId = afterTenantId,
                includeDeleted = false,
            )
            if (page.isEmpty()) break
            page.forEach { ids.add(it.tenantId) }
            val last = page.last()
            afterCreatedAt = last.createdAt
            afterTenantId = last.tenantId
            if (page.size < remaining) break
        }
        return ids
    }

    private fun counter(name: String, vararg tags: String) {
        meterRegistry?.counter(name, *tags)?.increment()
    }

    private companion object {
        const val PAGE_SIZE = 500
        val log = LoggerFactory.getLogger(AuditChainVerifyJob::class.java)
    }
}
