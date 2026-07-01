package ai.cypherx.auth.api

import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.UsageDocument
import ai.cypherx.auth.service.UsageRollupService
import org.springframework.format.annotation.DateTimeFormat
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController
import java.time.Instant

/**
 * Per-tenant usage API (Component 1d / Contract 19 — WP04 rollup read).
 *
 *   GET /v1/usage   — the caller tenant's LLM usage rollup (requests / tokens-in / tokens-out /
 *                     cost-usd) as per-metric totals + the hourly series, optionally windowed by
 *                     `from`/`to` (ISO-8601).
 *
 * Required scope: `tenant:read` (or any admin scope), matching the self-service quota read in
 * [QuotaController]. The caller's tenant is taken from the verified JWT (Contract 13) — there is no
 * cross-tenant read here (a platform fleet view would be a separate platform-admin endpoint).
 *
 * Reads ONLY `auth.tenant_usage_counters` (the rollup fed by
 * [ai.cypherx.auth.service.UsageRollupConsumer]); it NEVER reaches across schemas into `llms.*`.
 *
 * Scope enforcement is in-handler via [CallerContext.requireAnyScope] (method-level security is not
 * enabled in the locked SecurityConfig); SecurityConfig already requires an authenticated principal
 * for this route. Errors are thrown as [ai.cypherx.auth.web.ApiException] → rendered by the Core
 * GlobalExceptionHandler.
 */
@RestController
@RequestMapping("/v1")
class UsageController(
    private val usageRollupService: UsageRollupService,
    private val callerContext: CallerContext,
) {

    @GetMapping("/usage")
    fun myUsage(
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) from: Instant?,
        @RequestParam(required = false) @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) to: Instant?,
    ): Map<String, Any?> {
        val caller = callerContext.requireAnyScope(
            SCOPE_TENANT_READ, SCOPE_TENANT_ADMIN, SCOPE_PLATFORM_ADMIN,
        )
        return usageRollupService.usage(caller.tenantId, from, to).toResponse()
    }

    /** Map the rollup document to the snake_case response body (Contract 19 shape). */
    private fun UsageDocument.toResponse(): Map<String, Any?> = linkedMapOf(
        "tenant_id" to tenantId.toString(),
        "from" to from?.toString(),
        "to" to to?.toString(),
        "totals" to totals.mapValues { it.value.stripTrailingZeros().toPlainString() },
        "series" to series.map {
            linkedMapOf(
                "window_start" to it.windowStart.toString(),
                "metric" to it.metric,
                "value" to it.value.stripTrailingZeros().toPlainString(),
            )
        },
    )

    private companion object {
        const val SCOPE_TENANT_READ = "tenant:read"
        const val SCOPE_TENANT_ADMIN = "tenant:admin"
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
    }
}
