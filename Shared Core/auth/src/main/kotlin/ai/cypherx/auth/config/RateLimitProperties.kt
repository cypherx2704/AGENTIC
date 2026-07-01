package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.ratelimit.*` configuration tree (WP03 — Component 4:
 * the SELF-PROTECTION rate-limit filter in front of Auth's OWN endpoints). Bound automatically by
 * @ConfigurationPropertiesScan on [ai.cypherx.auth.AuthApplication].
 *
 * The PER-ENDPOINT LIMITS themselves are NOT here — they live in the DB table
 * `auth.rate_limit_config` (loaded by [ai.cypherx.auth.repo.RateLimitConfigRepository] and cached by
 * [ai.cypherx.auth.web.RateLimitFilter]). This class only holds the operational knobs of the
 * limiter machinery. Every value is env-overridable (e.g. `CYPHERX_AUTH_RATELIMIT_ENABLED`); the
 * in-code defaults are the documented last-resort fallbacks — nothing here is a hardcoded tunable.
 *
 * The limiter mirrors the [ai.cypherx.auth.service.RevocationChecker] idiom: a Valkey fixed-window
 * counter that FAILS OPEN — if Valkey is absent/slow/erroring it ACCEPTS the request but still
 * enforces a coarse in-process backstop ([inProcessHardCeilingRpm]) so a Valkey outage can't let
 * unbounded traffic through. Fail-open is observable via `auth_rate_limit_failopen_total` and a
 * `rate_limit_failopen=true` log line.
 */
@ConfigurationProperties(prefix = "cypherx.auth.ratelimit")
data class RateLimitProperties(

    /** Master switch. Off → the filter passes every request straight through (no limiting at all). */
    val enabled: Boolean = true,

    /**
     * Hard cap (ms) on a single Valkey INCR+EXPIRE round-trip. If the op throws OR exceeds this, the
     * limiter FAILS OPEN for that request (accepts it, falls back to the in-process backstop). Keep
     * it small so a slow Valkey never stalls a request on the hot path. Should sit under the
     * `spring.data.redis.timeout` so the connection-level timeout is the outer bound.
     */
    val valkeyTimeoutMs: Long = 50,

    /**
     * Last-resort in-process backstop ceiling (requests/minute, GLOBAL across all scopes/keys for
     * this instance) applied ONLY while failing open (Valkey unavailable). It is intentionally
     * coarse and generous — its sole job is to stop truly unbounded traffic during a cache outage,
     * NOT to enforce the precise per-scope DB limits. Set high enough not to clip normal traffic.
     */
    val inProcessHardCeilingRpm: Int = 20_000,

    /**
     * How often (seconds) the cached `auth.rate_limit_config` snapshot is reloaded from the DB. The
     * filter serves every request from the in-memory snapshot and never hits the DB on the hot path;
     * a refresh older than this triggers a single reload (config edits take up to this long to apply).
     */
    val configRefreshSeconds: Long = 60,

    /**
     * Valkey key namespace for the fixed-window counters. Distinct from the revocation prefix
     * (`cypherx:rev:`) so the two schemes never collide. Keys are
     * `<prefix>{endpoint}:{scopeKind}:{id}:{windowEpoch}`.
     */
    val keyPrefix: String = "cypherx:auth:rl:",

    /**
     * Fixed-window length (seconds) for the per-minute (rpm) limits. 60 = one-minute windows aligned
     * to the wall clock (`floor(epochSecond / windowSeconds)`). Exposed for tests / tuning; the DB
     * limits are expressed as rpm, so changing this also rescales the effective per-window allowance.
     */
    val windowSeconds: Long = 60,
)
