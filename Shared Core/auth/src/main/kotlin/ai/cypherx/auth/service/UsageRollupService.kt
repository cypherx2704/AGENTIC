package ai.cypherx.auth.service

import ai.cypherx.auth.repo.TenantUsageCounter
import ai.cypherx.auth.repo.TenantUsageCounterRepository
import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.math.BigDecimal
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.UUID

/**
 * Rolls `cypherx.llms.usage.recorded` (Contract 19) events into `auth.tenant_usage_counters` and
 * resolves the `/v1/usage` read (Component 1d — WP04).
 *
 * Rollup model: each usage event carries a tenant + token/request/cost figures for one LLM call.
 * We bucket by the event's hour (UTC-truncated `window_start`) and UPSERT-increment four metrics:
 *  - `llm_requests`   (+1 per event)
 *  - `llm_tokens_in`  (+prompt/input tokens)
 *  - `llm_tokens_out` (+completion/output tokens)
 *  - `llm_cost_usd`   (+cost in USD)
 *
 * The consumer ([ai.cypherx.auth.service.UsageRollupConsumer]) hands the parsed Contract 5 envelope's
 * `payload` here. Parsing is lenient about field naming (the producer's exact field names may vary by
 * Contract 19 revision) so a benign rename does not silently drop usage — we accept a small set of
 * documented aliases for each figure.
 *
 * `/v1/usage` reads ONLY this rollup (no cross-schema reads into `llms.*`).
 */
@Service
class UsageRollupService(
    private val usageCounters: TenantUsageCounterRepository,
    private val objectMapper: ObjectMapper,
) {

    /** Metric names persisted in `auth.tenant_usage_counters.metric` (match the WP03 0004 comment). */
    object Metrics {
        const val REQUESTS = "llm_requests"
        const val TOKENS_IN = "llm_tokens_in"
        const val TOKENS_OUT = "llm_tokens_out"
        const val COST_USD = "llm_cost_usd"
    }

    // ── Rollup (consumer side) ──────────────────────────────────────────────────────────────────

    /**
     * Apply one usage event payload to the per-tenant hourly counters. [payload] is the Contract 5
     * envelope's `payload` object (already extracted by the consumer). [eventTenantId] is the
     * envelope `tenant_id` (authoritative); the payload's `tenant_id` is used as a fallback.
     *
     * Returns true when at least one counter was incremented; false when the event lacked a usable
     * tenant or had no positive figures (so the consumer can log/skip without failing the partition).
     */
    fun applyUsageEvent(payload: JsonNode, eventTenantId: UUID?, producedAt: Instant?): Boolean {
        val tenantId = eventTenantId
            ?: payload.path("tenant_id").asText(null)?.let { runCatching { UUID.fromString(it) }.getOrNull() }
            ?: run {
                log.warn("usage event has no resolvable tenant_id — skipped")
                return false
            }

        val tsRaw = payload.path("recorded_at").asText(null)
            ?: payload.path("occurred_at").asText(null)
            ?: payload.path("timestamp").asText(null)
        val ts = tsRaw?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: producedAt ?: Instant.now()
        val windowStart = ts.truncatedTo(ChronoUnit.HOURS)

        val tokensIn = firstDecimal(payload, "tokens_in", "input_tokens", "prompt_tokens", "tokens_input")
        val tokensOut = firstDecimal(payload, "tokens_out", "output_tokens", "completion_tokens", "tokens_output")
        val costUsd = firstDecimal(payload, "cost_usd", "cost", "total_cost_usd", "estimated_cost_usd")
        // Some producers carry an explicit per-event request count; default to 1 request per event.
        val requests = firstDecimal(payload, "requests", "request_count")?.takeIf { it.signum() > 0 } ?: BigDecimal.ONE

        var applied = false
        applied = incIfPositive(tenantId, windowStart, Metrics.REQUESTS, requests) || applied
        tokensIn?.let { applied = incIfPositive(tenantId, windowStart, Metrics.TOKENS_IN, it) || applied }
        tokensOut?.let { applied = incIfPositive(tenantId, windowStart, Metrics.TOKENS_OUT, it) || applied }
        costUsd?.let { applied = incIfPositive(tenantId, windowStart, Metrics.COST_USD, it) || applied }

        if (!applied) log.debug("usage event for tenant {} had no positive figures — nothing rolled up", tenantId)
        return applied
    }

    /**
     * Parse a raw Contract 5 envelope JSON string and apply it. Returns true on a successful rollup.
     * Malformed JSON is logged and skipped (returns false) so a poison message does not wedge the
     * partition — the producer/DLQ owns retry of truly bad records.
     */
    fun applyEnvelopeJson(json: String): Boolean {
        val root = try {
            objectMapper.readTree(json)
        } catch (ex: Exception) {
            log.warn("usage event is not valid JSON — skipped: {}", ex.message)
            return false
        }
        val payload = root.path("payload").takeIf { it.isObject } ?: root
        val tenantId = root.path("tenant_id").asText(null)
            ?.let { runCatching { UUID.fromString(it) }.getOrNull() }
        val producedAt = root.path("produced_at").asText(null)
            ?.let { runCatching { Instant.parse(it) }.getOrNull() }
        return applyUsageEvent(payload, tenantId, producedAt)
    }

    // ── Read (`/v1/usage`) ──────────────────────────────────────────────────────────────────────

    /**
     * Resolve the `/v1/usage` document for [tenantId] over the window [from, to]: per-metric totals
     * plus the hourly series. Reads ONLY `auth.tenant_usage_counters` (no cross-schema read).
     */
    fun usage(tenantId: UUID, from: Instant?, to: Instant?): UsageDocument {
        val rows: List<TenantUsageCounter> = usageCounters.read(tenantId, from, to)
        val totals = linkedMapOf<String, BigDecimal>()
        val series = rows.map { row ->
            totals.merge(row.metric, row.value, BigDecimal::add)
            UsageBucket(windowStart = row.windowStart, metric = row.metric, value = row.value)
        }
        return UsageDocument(
            tenantId = tenantId,
            from = from,
            to = to,
            totals = totals,
            series = series,
        )
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    private fun incIfPositive(tenantId: UUID, windowStart: Instant, metric: String, delta: BigDecimal): Boolean {
        if (delta.signum() <= 0) return false
        usageCounters.increment(tenantId, windowStart, metric, delta)
        return true
    }

    /** First of [names] present and numeric-parseable in [node], else null. */
    private fun firstDecimal(node: JsonNode, vararg names: String): BigDecimal? {
        for (name in names) {
            val v = node.get(name) ?: continue
            if (v.isNull) continue
            val parsed = when {
                v.isNumber -> v.decimalValue()
                v.isTextual -> runCatching { BigDecimal(v.asText().trim()) }.getOrNull()
                else -> null
            }
            if (parsed != null) return parsed
        }
        return null
    }

    private companion object {
        val log = LoggerFactory.getLogger(UsageRollupService::class.java)
    }
}

/** One hourly usage bucket surfaced to `/v1/usage`. */
data class UsageBucket(
    val windowStart: Instant,
    val metric: String,
    val value: BigDecimal,
)

/** The `/v1/usage` response document: per-metric totals + the hourly series for the window. */
data class UsageDocument(
    val tenantId: UUID,
    val from: Instant?,
    val to: Instant?,
    val totals: Map<String, BigDecimal>,
    val series: List<UsageBucket>,
)
