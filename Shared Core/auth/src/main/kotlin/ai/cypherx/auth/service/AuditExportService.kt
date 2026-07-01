package ai.cypherx.auth.service

import ai.cypherx.auth.config.AuditPipelineProperties
import ai.cypherx.auth.repo.AuditExportJobRepository
import ai.cypherx.auth.repo.AuditRepository
import ai.cypherx.auth.service.s3.ObjectStore
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.io.ByteArrayOutputStream
import java.time.Duration
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Streams a tenant's `auth.audit_log` to object storage as JSONL and returns a presigned download
 * URL (Component 6 export — WP04).
 *
 * Flow:
 *  1. Page the tenant's audit rows via [AuditService.list] (keyset cursor; RLS-confined) — we never
 *     re-implement the read path or touch the chain hashing (delegated to the existing service).
 *  2. Serialise each row as one JSON object per line (JSONL) — the canonical interchange format for
 *     a log export (line-oriented, append-friendly, trivially re-ingestible).
 *  3. Upload to the pluggable [ObjectStore] (S3/MinIO in prod, local filesystem in dev) under
 *     `{exportKeyPrefix}/{tenant}/{ts}.jsonl`.
 *  4. Return a presigned GET URL with the configured TTL (Contract default: 7 days).
 *
 * FAIL-SOFT: when the object store is unconfigured ([ObjectStore.isConfigured] == false) the export
 * raises 503 (the caller gets a clean Contract 2 error) rather than the app failing to boot — so a
 * local/test boot with no object backend still starts.
 *
 * Each export row carries the full audited columns AND the tamper-evidence hashes (`row_hash`,
 * `prev_row_hash`) hex-encoded, so an external auditor can re-walk the chain from the export alone.
 */
@Service
class AuditExportService(
    private val auditService: AuditService,
    private val objectStore: ObjectStore,
    private val props: AuditPipelineProperties,
    private val objectMapper: ObjectMapper,
    private val exportJobRepository: AuditExportJobRepository,
) {

    /** Outcome of an export: where it landed, how many rows, and the presigned download URL. */
    data class ExportResult(
        val exportId: UUID?,
        val tenantId: UUID,
        val objectKey: String,
        val objectUri: String,
        val rowCount: Long,
        val truncated: Boolean,
        val downloadUrl: String,
        val expiresAt: Instant,
        val backend: String,
    )

    /**
     * Export [tenantId]'s audit log (optionally windowed by [from]/[to]) to object storage and return
     * a presigned URL. Streams in keyset-paginated batches so memory stays bounded; caps the total at
     * [AuditPipelineProperties.exportMaxRows] (sets `truncated=true` if hit). [requestedBy] is the
     * acting admin agent id, recorded on the `auth.audit_export_jobs` audit-trail row.
     *
     * @throws ApiException 503 when the object store is unconfigured, or 500 on a write/sign failure.
     */
    fun export(tenantId: UUID, from: Instant?, to: Instant?, requestedBy: UUID?): ExportResult {
        if (!objectStore.isConfigured) {
            throw ApiException.serviceUnavailable(
                "Audit export object store is not configured",
                mapOf("store" to objectStore.backend),
            )
        }

        val now = Instant.now()
        val key = "${props.exportKeyPrefix}/$tenantId/${OBJECT_TS_FMT.format(now)}.jsonl"
        val maxRows = props.exportMaxRows

        // Build the JSONL body, paging the read path. Audit exports are bounded by exportMaxRows so a
        // bounded buffer is acceptable here (the object store PUT needs the exact content length).
        val buffer = ByteArrayOutputStream()
        var rowCount = 0L
        var truncated = false
        var afterId: Long? = null

        while (rowCount < maxRows) {
            val remaining = (maxRows - rowCount).coerceAtMost(PAGE_SIZE.toLong()).toInt()
            val rows = auditService.list(
                tenantId = tenantId,
                from = from,
                to = to,
                eventType = null,
                agentId = null,
                afterId = afterId,
                limit = remaining,
            )
            if (rows.isEmpty()) break
            for (row in rows) {
                val line = objectMapper.writeValueAsString(row.toExportMap())
                buffer.write(line.toByteArray(Charsets.UTF_8))
                buffer.write('\n'.code)
            }
            rowCount += rows.size
            afterId = rows.last().id
            if (rows.size < remaining) break
            if (rowCount >= maxRows) {
                // Hit the cap on a full page — there may be more rows we deliberately did not export.
                truncated = true
            }
        }

        val bytes = buffer.toByteArray()
        val ttl = Duration.ofSeconds(props.exportUrlTtlSeconds)
        val objectUri = try {
            objectStore.putBytes(key, bytes, CONTENT_TYPE_JSONL)
        } catch (ex: ObjectStore.ObjectStoreException) {
            log.error("audit export write failed for tenant {} key {}: {}", tenantId, key, ex.message)
            throw ApiException.internal("Audit export upload failed")
        }
        val downloadUrl = try {
            objectStore.presignedGetUrl(key, ttl)
        } catch (ex: ObjectStore.ObjectStoreException) {
            log.error("audit export presign failed for tenant {} key {}: {}", tenantId, key, ex.message)
            throw ApiException.internal("Audit export URL signing failed")
        }

        val expiresAt = now.plus(ttl)

        // Record the export-job audit trail (best-effort: the export already succeeded; a failure to
        // record the trail row must not turn a successful export into an error for the caller).
        val exportId = runCatching {
            exportJobRepository.record(
                AuditExportJobRepository.NewExportJob(
                    tenantId = tenantId,
                    requestedBy = requestedBy,
                    storeBackend = objectStore.backend,
                    objectKey = key,
                    objectUri = objectUri,
                    rowCount = rowCount,
                    truncated = truncated,
                    windowFrom = from,
                    windowTo = to,
                    urlExpiresAt = expiresAt,
                    status = "completed",
                ),
            )
        }.onFailure { log.warn("audit export job-record failed for tenant {} key {}: {}", tenantId, key, it.message) }
            .getOrNull()

        log.info(
            "audit export tenant={} rows={} truncated={} backend={} key={} export_id={}",
            tenantId, rowCount, truncated, objectStore.backend, key, exportId,
        )

        return ExportResult(
            exportId = exportId,
            tenantId = tenantId,
            objectKey = key,
            objectUri = objectUri,
            rowCount = rowCount,
            truncated = truncated,
            downloadUrl = downloadUrl,
            expiresAt = expiresAt,
            backend = objectStore.backend,
        )
    }

    /** One export line: the audited columns + hex-encoded tamper-evidence hashes (auditor-replayable). */
    private fun AuditRepository.AuditRow.toExportMap(): Map<String, Any?> = linkedMapOf(
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

    private companion object {
        const val PAGE_SIZE = 500
        const val CONTENT_TYPE_JSONL = "application/x-ndjson"

        val OBJECT_TS_FMT: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'").withZone(ZoneOffset.UTC)

        val log = LoggerFactory.getLogger(AuditExportService::class.java)
    }
}
