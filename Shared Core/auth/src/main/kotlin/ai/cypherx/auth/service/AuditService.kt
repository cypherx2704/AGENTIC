package ai.cypherx.auth.service

import ai.cypherx.auth.repo.AuditRepository
import ai.cypherx.auth.web.TraceContextFilter
import org.slf4j.MDC
import org.springframework.stereotype.Service
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.UUID

/**
 * Append-only audit writer with the per-tenant tamper-evident hash chain (Component 6, Phase 2).
 *
 * Used by AuthorizeService and every other write path (agent register, key issue/revoke, token
 * issue/revoke, tenant lifecycle) to record an immutable, verifiable trail in `auth.audit_log`.
 *
 * Hash chain (per tenant — independent chains avoid cross-tenant serialisation):
 *
 *     row_hash(N) = SHA-256( canonicalPayload(N) || prev_row_hash(N) )
 *
 * where `prev_row_hash` is the previous row's `row_hash` (genesis = 32 zero bytes). The canonical
 * payload is a deterministic, field-ordered, pipe-delimited string of exactly the columns the
 * schema enumerates (event_type|agent_id|tenant_id|action|resource|decision|policy_ids|request_id|
 * trace_id|ip_address|created_at). Stability matters: an external auditor MUST be able to replay
 * [canonicalPayload] byte-for-byte to re-derive each hash and detect any modification or deletion.
 *
 * `request_id`/`trace_id` are taken from the MDC ([TraceContextFilter]) when the caller does not
 * supply them, so audit rows carry the same correlation ids as the Contract 2 error envelope.
 */
@Service
class AuditService(
    private val auditRepository: AuditRepository,
) {

    /** Result of an append: new row id + the row_hash that is now the tenant chain tip. */
    data class AuditWrite(val id: Long, val rowHashHex: String, val prevRowHashHex: String)

    /**
     * Append one audit event. Any null correlation id falls back to the current MDC value. The full
     * insert (tip read + hash + insert) runs in a single tenant transaction inside the repository.
     */
    fun record(
        eventType: String,
        tenantId: UUID,
        agentId: UUID? = null,
        action: String? = null,
        resource: String? = null,
        decision: String? = null,
        policyIds: List<String> = emptyList(),
        requestId: UUID? = null,
        traceId: UUID? = null,
        ipAddress: String? = null,
        createdAt: Instant = Instant.now(),
    ): AuditWrite {
        val row = AuditRepository.NewAuditRow(
            eventType = eventType,
            agentId = agentId,
            tenantId = tenantId,
            action = action,
            resource = resource,
            decision = decision,
            policyIds = policyIds,
            requestId = requestId ?: mdcUuid(TraceContextFilter.MDC_REQUEST_ID),
            traceId = traceId ?: mdcTraceUuid(),
            ipAddress = ipAddress,
            // Truncate to the SAME precision the DB column stores (timestamptz = microseconds) so the
            // value we hash here is byte-for-byte the value persisted and later re-read by verifyChain
            // — a nanosecond `Instant.now()` would otherwise be rounded by Postgres and the chain would
            // appear "broken" on re-derivation. See [canonicalTimestamp] / AuditRepository.insert.
            createdAt = createdAt.truncatedTo(ChronoUnit.MICROS),
        )
        val appended = auditRepository.appendInTenant(tenantId, row) { prev, r -> computeRowHash(prev, r) }
        return AuditWrite(
            id = appended.id,
            rowHashHex = appended.rowHash.toHex(),
            prevRowHashHex = appended.prevRowHash.toHex(),
        )
    }

    /**
     * Re-walk the per-tenant hash chain over a window and report whether it is intact (Component 6
     * verify API). Recomputes each row_hash from its canonical payload + the prior row_hash and
     * compares; the first divergence is reported with the offending row id.
     */
    fun verifyChain(tenantId: UUID, from: Instant?, to: Instant?): ChainVerification {
        val rows = auditRepository.chain(tenantId, from, to)
        if (rows.isEmpty()) {
            return ChainVerification(ok = true, rowsVerified = 0, fromHashHex = null, toHashHex = null)
        }
        // The window may start mid-chain; anchor on the first row's stored prev so a windowed
        // verification still validates internal linkage and per-row hashes.
        var expectedPrev = rows.first().prevRowHash
        for (row in rows) {
            if (!row.prevRowHash.contentEquals(expectedPrev)) {
                return ChainVerification(
                    ok = false,
                    rowsVerified = 0,
                    fromHashHex = rows.first().prevRowHash.toHex(),
                    toHashHex = null,
                    brokenAtRowId = row.id,
                    expectedPrevHashHex = expectedPrev.toHex(),
                    actualPrevHashHex = row.prevRowHash.toHex(),
                )
            }
            val recomputed = computeRowHash(
                row.prevRowHash,
                AuditRepository.NewAuditRow(
                    eventType = row.eventType,
                    agentId = row.agentId,
                    tenantId = row.tenantId,
                    action = row.action,
                    resource = row.resource,
                    decision = row.decision,
                    policyIds = row.policyIds,
                    requestId = row.requestId,
                    traceId = row.traceId,
                    ipAddress = row.ipAddress,
                    createdAt = row.createdAt,
                ),
            )
            if (!recomputed.contentEquals(row.rowHash)) {
                return ChainVerification(
                    ok = false,
                    rowsVerified = 0,
                    fromHashHex = rows.first().prevRowHash.toHex(),
                    toHashHex = null,
                    brokenAtRowId = row.id,
                    expectedPrevHashHex = recomputed.toHex(),
                    actualPrevHashHex = row.rowHash.toHex(),
                )
            }
            expectedPrev = row.rowHash
        }
        return ChainVerification(
            ok = true,
            rowsVerified = rows.size.toLong(),
            fromHashHex = rows.first().prevRowHash.toHex(),
            toHashHex = rows.last().rowHash.toHex(),
        )
    }

    /** Paginated read for the Component 6 read API. Delegates to the repo (RLS enforces tenant). */
    fun list(
        tenantId: UUID,
        from: Instant?,
        to: Instant?,
        eventType: String?,
        agentId: UUID?,
        afterId: Long?,
        limit: Int,
    ): List<AuditRepository.AuditRow> =
        auditRepository.list(tenantId, from, to, eventType, agentId, afterId, limit)

    // ── Hashing ──────────────────────────────────────────────────────────────────────────

    /** row_hash = SHA-256( canonicalPayload || prev_row_hash ). */
    private fun computeRowHash(prevRowHash: ByteArray, row: AuditRepository.NewAuditRow): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        md.update(canonicalPayload(row).toByteArray(StandardCharsets.UTF_8))
        md.update(prevRowHash)
        return md.digest()
    }

    /**
     * Deterministic canonical serialisation of the audited columns. Field order and the NUL-safe
     * pipe delimiter are part of the contract — never reorder or change the null sentinel without a
     * coordinated re-hash, or every downstream verification breaks. Null → empty; lists → the
     * comma-joined element order as stored; timestamps → epoch-MICROSECONDS (the exact precision the
     * `timestamptz` column stores), so re-derivation from the DB-read value matches byte-for-byte.
     */
    fun canonicalPayload(row: AuditRepository.NewAuditRow): String =
        listOf(
            row.eventType,
            row.agentId?.toString() ?: "",
            row.tenantId.toString(),
            row.action ?: "",
            row.resource ?: "",
            row.decision ?: "",
            row.policyIds.joinToString(","),
            row.requestId?.toString() ?: "",
            row.traceId?.toString() ?: "",
            row.ipAddress ?: "",
            canonicalTimestamp(row.createdAt),
        ).joinToString("|")

    /**
     * Canonical timestamp form for the hash chain: epoch MICROSECONDS as a decimal string. Postgres
     * `timestamptz` persists at microsecond precision, so we hash over the truncated-to-micros value
     * on BOTH the write path (insert hashes this) and the read path (verifyChain re-hashes the value
     * read back from the column) — eliminating the millis-vs-micros mismatch that flagged untampered
     * chains as broken.
     */
    private fun canonicalTimestamp(instant: Instant): String {
        val micros = instant.truncatedTo(ChronoUnit.MICROS)
        return (micros.epochSecond * 1_000_000L + micros.nano / 1_000L).toString()
    }

    // ── MDC helpers ──────────────────────────────────────────────────────────────────────

    private fun mdcUuid(key: String): UUID? =
        MDC.get(key)?.takeIf { it.isNotBlank() }?.let { runCatching { UUID.fromString(it) }.getOrNull() }

    /** trace_id in the MDC is a 32-hex W3C id, not a UUID — parse it into a UUID for the uuid column. */
    private fun mdcTraceUuid(): UUID? {
        val raw = MDC.get(TraceContextFilter.MDC_TRACE_ID)?.takeIf { it.isNotBlank() } ?: return null
        runCatching { return UUID.fromString(raw) }
        if (raw.length == 32) {
            runCatching {
                return UUID.fromString(
                    "${raw.substring(0, 8)}-${raw.substring(8, 12)}-${raw.substring(12, 16)}-" +
                        "${raw.substring(16, 20)}-${raw.substring(20)}",
                )
            }
        }
        return null
    }

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

    /** Outcome of [verifyChain]. */
    data class ChainVerification(
        val ok: Boolean,
        val rowsVerified: Long,
        val fromHashHex: String?,
        val toHashHex: String?,
        val brokenAtRowId: Long? = null,
        val expectedPrevHashHex: String? = null,
        val actualPrevHashHex: String? = null,
    )
}
