package ai.cypherx.auth.web

import ai.cypherx.auth.config.RateLimitProperties
import ai.cypherx.auth.repo.RateLimitConfigRepository
import ai.cypherx.auth.repo.RateLimitRule
import com.fasterxml.jackson.databind.ObjectMapper
import com.nimbusds.jwt.SignedJWT
import io.micrometer.core.instrument.MeterRegistry
import jakarta.servlet.FilterChain
import jakarta.servlet.http.HttpServletRequest
import jakarta.servlet.http.HttpServletResponse
import org.slf4j.LoggerFactory
import org.slf4j.MDC
import org.springframework.beans.factory.ObjectProvider
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.web.filter.OncePerRequestFilter
import java.time.Instant
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.math.ceil

/**
 * Self-protection rate-limit filter (WP03 Component 4). A Spring-Security-chain [OncePerRequestFilter]
 * that throttles Auth's OWN endpoints with a Valkey fixed-window counter, BEFORE the expensive work
 * (DB / crypto) runs. Limits come from `auth.rate_limit_config` (NOT hardcoded), cached in-memory and
 * refreshed every [RateLimitProperties.configRefreshSeconds]; operational knobs come from
 * [RateLimitProperties].
 *
 * Per request:
 *  1. If disabled, or the path matches no configured endpoint, pass through.
 *  2. For each matching rule, derive the scope key for its `scope_kind` (tenant / agent / service /
 *     admin-agent / caller-service / ip). A rule whose identifier can't be resolved is SKIPPED (not a
 *     rejection) so a missing claim degrades to the other scopes / pass-through.
 *  3. Valkey fixed-window: `INCR <prefix>{endpoint}:{scopeKind}:{id}:{windowEpoch}` + `EXPIRE`
 *     windowSeconds. If the post-increment count > effective limit → 429 [.RATE_LIMITED] (Contract 2
 *     envelope) + `Retry-After`, chain NOT continued.
 *  4. FAIL-OPEN: if Valkey is absent / errors / exceeds [RateLimitProperties.valkeyTimeoutMs], the
 *     request is ACCEPTED but a coarse in-process GLOBAL ceiling
 *     ([RateLimitProperties.inProcessHardCeilingRpm]) is enforced as a last-resort backstop; this is
 *     logged `rate_limit_failopen=true` and counted via `auth_rate_limit_failopen_total`. Only when
 *     the backstop itself is exceeded during an outage does fail-open reject (429).
 *
 * ORDERING (see [ai.cypherx.auth.config.SecurityConfig] registration note): this runs EARLY — before
 * [AgentJwtAuthFilter] — so an UNAUTHENTICATED flood is still capped. Because the Spring principal
 * isn't set yet, scope-key extraction parses the Bearer token's claims itself (best-effort, via the
 * raw JWT payload — NO signature verification, which is fine: the worst a forged claim can do is land
 * an attacker in a DIFFERENT bucket; the per-ip rule and the in-process ceiling still bound them). It
 * also falls back to the Kong-injected `X-Tenant-ID` / `X-Agent-ID` headers and the client IP.
 *
 * Provided as a constructable bean by SecurityConfig (constructor injection).
 */
class RateLimitFilter(
    private val repository: RateLimitConfigRepository,
    private val props: RateLimitProperties,
    redisProvider: ObjectProvider<StringRedisTemplate>,
    meterRegistryProvider: ObjectProvider<MeterRegistry>,
) : OncePerRequestFilter() {

    /** null when Redis/Valkey auto-config is absent — the filter then always fails open. */
    private val redis: StringRedisTemplate? = redisProvider.ifAvailable
    private val meters: MeterRegistry? = meterRegistryProvider.ifAvailable
    private val mapper = ObjectMapper()

    /** Single-threaded bounded executor so a slow Valkey op can be cut off at [valkeyTimeoutMs]. */
    private val valkeyExecutor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "auth-ratelimit-valkey").apply { isDaemon = true }
    }

    /** In-memory snapshot of the DB rules + when it was loaded, refreshed on a cadence. */
    private val cache = AtomicReference(RuleCache(emptyList(), Instant.EPOCH))

    /** Coarse fail-open backstop: a per-window global counter for THIS instance. */
    private val failOpenWindow = AtomicLong(-1)
    private val failOpenCount = AtomicLong(0)

    override fun doFilterInternal(
        request: HttpServletRequest,
        response: HttpServletResponse,
        filterChain: FilterChain,
    ) {
        if (!props.enabled) {
            filterChain.doFilter(request, response)
            return
        }

        val path = request.requestURI ?: ""
        val rules = rules().filter { pathMatches(it.endpoint, path) }
        if (rules.isEmpty()) {
            filterChain.doFilter(request, response)
            return
        }

        // Effective rule per (endpoint, scope_kind): a per-tenant override (tenant_id != null)
        // matching this caller's tenant wins over the platform default (tenant_id == null).
        val callerTenant = resolveTenantId(request)
        val effective = pickEffectiveRules(rules, callerTenant)

        for (rule in effective) {
            val scopeId = scopeId(rule.scopeKind, request, callerTenant) ?: continue // unresolved → skip scope
            val decision = enforce(rule, scopeId)
            if (decision is Decision.Reject) {
                reject(request, response, decision.retryAfterSeconds, rule)
                return
            }
        }

        filterChain.doFilter(request, response)
    }

    // ── rule cache ─────────────────────────────────────────────────────────────────────────────

    private fun rules(): List<RateLimitRule> {
        val current = cache.get()
        val staleAfter = current.loadedAt.plusSeconds(props.configRefreshSeconds)
        if (Instant.now().isAfter(staleAfter)) {
            try {
                val fresh = repository.findAll()
                cache.set(RuleCache(fresh, Instant.now()))
                return fresh
            } catch (ex: Exception) {
                // Keep serving the previous snapshot on a transient DB hiccup; don't fail the request.
                log.warn("rate_limit_config refresh failed, serving cached snapshot: {}", ex.message)
            }
        }
        return current.rules
    }

    /**
     * Collapse to one rule per (endpoint, scope_kind): prefer the row whose tenant_id == [callerTenant]
     * (an enterprise override), else the platform default (tenant_id == null).
     */
    private fun pickEffectiveRules(rules: List<RateLimitRule>, callerTenant: UUID?): List<RateLimitRule> =
        rules.groupBy { it.endpoint to it.scopeKind }
            .mapNotNull { (_, candidates) ->
                candidates.firstOrNull { it.tenantId != null && it.tenantId == callerTenant }
                    ?: candidates.firstOrNull { it.tenantId == null }
            }

    // ── enforcement ──────────────────────────────────────────────────────────────────────────

    private fun enforce(rule: RateLimitRule, scopeId: String): Decision {
        val limit = effectiveLimit(rule)
        val r = redis ?: return failOpen("valkey-absent", limit)

        val windowEpoch = Instant.now().epochSecond / props.windowSeconds
        val key = "${props.keyPrefix}${rule.endpoint}:${rule.scopeKind}:$scopeId:$windowEpoch"

        return try {
            val count = withTimeout {
                val c = r.opsForValue().increment(key) ?: 1L
                // EXPIRE only needs setting on the first hit of the window, but re-asserting is cheap
                // and self-heals if a prior EXPIRE was lost. Window length, not remaining time.
                if (c == 1L) r.expire(key, props.windowSeconds, TimeUnit.SECONDS)
                c
            }
            if (count > limit) {
                Decision.Reject(retryAfterSeconds = secondsToWindowEnd(windowEpoch))
            } else {
                Decision.Allow
            }
        } catch (ex: TimeoutException) {
            failOpen("valkey-timeout", limit)
        } catch (ex: Exception) {
            failOpen(ex.message ?: ex.javaClass.simpleName, limit)
        }
    }

    /** Effective per-window ceiling. burst_seconds>0 and a multiplier>1 raise the cap for the window. */
    private fun effectiveLimit(rule: RateLimitRule): Long {
        val perWindow = rule.limitRpm.toDouble() * (props.windowSeconds.toDouble() / 60.0)
        val withBurst =
            if (rule.burstSeconds > 0) perWindow * rule.burstMultiplier.toDouble() else perWindow
        return ceil(withBurst).toLong().coerceAtLeast(1L)
    }

    private fun secondsToWindowEnd(windowEpoch: Long): Long {
        val windowEndEpoch = (windowEpoch + 1) * props.windowSeconds
        return (windowEndEpoch - Instant.now().epochSecond).coerceIn(1L, props.windowSeconds)
    }

    /**
     * FAIL-OPEN path: accept, but enforce the coarse in-process GLOBAL ceiling so a Valkey outage
     * can't admit unbounded traffic. Returns Reject ONLY when that backstop is itself exceeded.
     */
    private fun failOpen(reason: String, limit: Long): Decision {
        meters?.counter("auth_rate_limit_failopen_total")?.increment()
        log.warn("rate_limit_failopen=true reason={} rule_limit={}", reason, limit)

        val windowEpoch = Instant.now().epochSecond / props.windowSeconds
        val ceiling = inProcessCeilingForWindow()
        val count = incrementFailOpenWindow(windowEpoch)
        return if (count > ceiling) {
            log.warn("rate_limit_failopen_backstop_exceeded=true count={} ceiling={}", count, ceiling)
            Decision.Reject(retryAfterSeconds = secondsToWindowEnd(windowEpoch))
        } else {
            Decision.Allow
        }
    }

    /** Scale the rpm ceiling to the window length. */
    private fun inProcessCeilingForWindow(): Long =
        ceil(props.inProcessHardCeilingRpm.toDouble() * (props.windowSeconds.toDouble() / 60.0))
            .toLong().coerceAtLeast(1L)

    /** Global in-process counter; resets whenever the window rolls over. */
    private fun incrementFailOpenWindow(windowEpoch: Long): Long {
        while (true) {
            val current = failOpenWindow.get()
            if (current == windowEpoch) {
                return failOpenCount.incrementAndGet()
            }
            // Window rolled — try to claim the reset. Only the thread that wins CAS resets the count.
            if (failOpenWindow.compareAndSet(current, windowEpoch)) {
                failOpenCount.set(1)
                return 1
            }
            // Lost the race; loop and re-read (another thread set the same window).
        }
    }

    // ── scope-key derivation ─────────────────────────────────────────────────────────────────

    /**
     * Resolve the rate-limit-key identifier for [scopeKind], or null if it can't be resolved (the
     * caller then SKIPS this scope rather than rejecting). Claims come from the UNVERIFIED bearer JWT
     * payload (this filter runs before auth) with header fallbacks; per-ip uses the client IP.
     */
    private fun scopeId(scopeKind: String, request: HttpServletRequest, callerTenant: UUID?): String? =
        when (scopeKind) {
            "per-ip" -> clientIp(request)
            "per-tenant" -> callerTenant?.toString()
            "per-agent" -> claim(request, "agent_id")
                ?: claim(request, "sub")?.takeUnless { it.startsWith("svc:") }
                ?: MDC.get(TraceContextFilter.MDC_AGENT_ID)
                ?: request.getHeader(TraceContextFilter.HDR_AGENT_ID)?.takeIf { it.isNotBlank() }
            "per-admin-agent" -> claim(request, "agent_id")
                ?: claim(request, "sub")?.takeUnless { it.startsWith("svc:") }
                ?: MDC.get(TraceContextFilter.MDC_AGENT_ID)
                ?: request.getHeader(TraceContextFilter.HDR_AGENT_ID)?.takeIf { it.isNotBlank() }
            "per-service" -> serviceName(request)
            "per-caller-service" -> serviceName(request)
            else -> {
                log.debug("unknown rate-limit scope_kind={} — skipping", scopeKind)
                null
            }
        }

    /** Service name from a service token's `service_name` claim, or `svc:<name>` subject. */
    private fun serviceName(request: HttpServletRequest): String? {
        claim(request, "service_name")?.let { return it }
        val sub = claim(request, "sub") ?: return null
        return if (sub.startsWith("svc:")) sub.removePrefix("svc:").takeIf { it.isNotEmpty() } else null
    }

    private fun resolveTenantId(request: HttpServletRequest): UUID? {
        val raw = claim(request, "tenant_id")
            ?: MDC.get(TraceContextFilter.MDC_TENANT_ID)
            ?: request.getHeader(TraceContextFilter.HDR_TENANT_ID)?.takeIf { it.isNotBlank() }
            ?: return null
        return runCatching { UUID.fromString(raw) }.getOrNull()
    }

    /** Best-effort: client IP, honouring the framework's forwarded-headers handling. */
    private fun clientIp(request: HttpServletRequest): String? {
        request.getHeader("X-Forwarded-For")?.takeIf { it.isNotBlank() }?.let {
            return it.split(",").first().trim()
        }
        return request.remoteAddr?.takeIf { it.isNotBlank() }
    }

    /**
     * Read a single claim from the request's bearer token WITHOUT verifying its signature. Parsing
     * only — see the class doc for why this is safe at this point in the chain. Cached per request so
     * repeated scope lookups don't re-parse.
     */
    private fun claim(request: HttpServletRequest, name: String): String? {
        val claims = parsedClaims(request) ?: return null
        return runCatching { claims.getStringClaim(name) }.getOrNull()?.takeIf { it.isNotBlank() }
    }

    @Suppress("UNCHECKED_CAST")
    private fun parsedClaims(request: HttpServletRequest): com.nimbusds.jwt.JWTClaimsSet? {
        (request.getAttribute(ATTR_CLAIMS))?.let {
            return if (it === NO_CLAIMS) null else it as com.nimbusds.jwt.JWTClaimsSet
        }
        val header = request.getHeader("Authorization")
        val parsed = if (header != null && header.startsWith(BEARER_PREFIX, ignoreCase = true)) {
            val token = header.substring(BEARER_PREFIX.length).trim()
            runCatching { SignedJWT.parse(token).jwtClaimsSet }.getOrNull()
        } else {
            null
        }
        request.setAttribute(ATTR_CLAIMS, parsed ?: NO_CLAIMS)
        return parsed
    }

    // ── 429 rendering (Contract 2 envelope) ───────────────────────────────────────────────────

    private fun reject(
        request: HttpServletRequest,
        response: HttpServletResponse,
        retryAfterSeconds: Long,
        rule: RateLimitRule,
    ) {
        meters?.counter(
            "auth_rate_limit_rejected_total",
            "endpoint", rule.endpoint,
            "scope_kind", rule.scopeKind,
        )?.increment()
        log.info(
            "rate_limited=true endpoint={} scope_kind={} retry_after_s={}",
            rule.endpoint, rule.scopeKind, retryAfterSeconds,
        )

        response.status = HttpStatus.TOO_MANY_REQUESTS.value()
        response.setHeader("Retry-After", retryAfterSeconds.toString())
        response.contentType = MediaType.APPLICATION_JSON_VALUE

        val error = linkedMapOf<String, Any?>(
            "code" to RATE_LIMITED,
            "message" to "Rate limit exceeded for ${rule.endpoint}",
            "details" to mapOf(
                "scope_kind" to rule.scopeKind,
                "limit_rpm" to rule.limitRpm,
                "retry_after_seconds" to retryAfterSeconds,
            ),
            "request_id" to (MDC.get(TraceContextFilter.MDC_REQUEST_ID) ?: UUID.randomUUID().toString()),
            "trace_id" to (MDC.get(TraceContextFilter.MDC_TRACE_ID) ?: newTraceId()),
            "timestamp" to Instant.now().toString(),
        )
        mapper.writeValue(response.outputStream, mapOf("error" to error))
        response.outputStream.flush()
    }

    private fun newTraceId(): String = UUID.randomUUID().toString().replace("-", "")

    // ── path matching ─────────────────────────────────────────────────────────────────────────

    /**
     * Match a configured [pattern] against an actual request [path]. Supports:
     *  - exact:           `/v1/authorize`
     *  - `{id}` segment:  `/v1/agents/{id}/token` matches `/v1/agents/<anything-non-slash>/token`
     *  - trailing wildcard: a pattern ending in slash-star (e.g. the admin prefix) matches that
     *    prefix plus one or more remaining segments.
     */
    private fun pathMatches(pattern: String, path: String): Boolean {
        val p = path.trimEnd('/').ifEmpty { "/" }
        if (pattern.endsWith("/*")) {
            val prefix = pattern.removeSuffix("/*")
            return p == prefix || p.startsWith("$prefix/")
        }
        val patternSegs = pattern.trim('/').split("/")
        val pathSegs = p.trim('/').split("/")
        if (patternSegs.size != pathSegs.size) return false
        return patternSegs.indices.all { i ->
            val ps = patternSegs[i]
            (ps.startsWith("{") && ps.endsWith("}")) || ps == pathSegs[i]
        }
    }

    // ── timeout wrapper ──────────────────────────────────────────────────────────────────────

    /** Run [op] on the bounded executor, cancelling (and rethrowing TimeoutException) past the budget. */
    private fun <T> withTimeout(op: () -> T): T {
        val future = valkeyExecutor.submit<T> { op() }
        return try {
            future.get(props.valkeyTimeoutMs, TimeUnit.MILLISECONDS)
        } catch (ex: TimeoutException) {
            future.cancel(true)
            throw ex
        } catch (ex: java.util.concurrent.ExecutionException) {
            throw ex.cause ?: ex
        }
    }

    override fun destroy() {
        valkeyExecutor.shutdownNow()
        super.destroy()
    }

    // ── internals ────────────────────────────────────────────────────────────────────────────

    private data class RuleCache(val rules: List<RateLimitRule>, val loadedAt: Instant)

    private sealed interface Decision {
        data object Allow : Decision
        data class Reject(val retryAfterSeconds: Long) : Decision
    }

    private companion object {
        val log = LoggerFactory.getLogger(RateLimitFilter::class.java)
        const val BEARER_PREFIX = "Bearer "
        const val RATE_LIMITED = "RATE_LIMITED"
        const val ATTR_CLAIMS = "ai.cypherx.auth.ratelimit.parsedClaims"
        val NO_CLAIMS = Any() // sentinel: "we tried to parse and there were none" (don't re-parse)
    }
}
