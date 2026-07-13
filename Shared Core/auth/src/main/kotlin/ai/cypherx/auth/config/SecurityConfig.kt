package ai.cypherx.auth.config

import ai.cypherx.auth.config.RateLimitProperties
import ai.cypherx.auth.repo.RateLimitConfigRepository
import ai.cypherx.auth.service.RevocationChecker
import ai.cypherx.auth.signing.JwtMintService
import ai.cypherx.auth.web.AgentJwtAuthFilter
import ai.cypherx.auth.web.ContractAccessDeniedHandler
import ai.cypherx.auth.web.ContractAuthenticationEntryPoint
import ai.cypherx.auth.web.RateLimitFilter
import io.micrometer.core.instrument.MeterRegistry
import org.springframework.beans.factory.ObjectProvider
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.http.HttpMethod
import org.springframework.security.config.annotation.web.builders.HttpSecurity
import org.springframework.security.config.annotation.web.invoke
import org.springframework.security.config.http.SessionCreationPolicy
import org.springframework.security.web.SecurityFilterChain
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter

/**
 * Spring Security wiring for the auth-service.
 *
 *  - Stateless (no HTTP session); CSRF disabled (token-auth API, no cookies).
 *  - permitAll for the public surface: well-known docs, metrics, health, the OAuth2 token
 *    endpoint, self-serve onboarding signup/verify, the one-time bootstrap, the agent/service
 *    token mints (those endpoints body-authenticate via api_key / bootstrap secret themselves).
 *  - Everything else requires authentication (an agent JWT established by [AgentJwtAuthFilter]).
 *    Fine-grained scope checks happen per-endpoint (e.g. `hasAuthority("SCOPE_platform:admin")`).
 *  - [AgentJwtAuthFilter] runs before UsernamePasswordAuthenticationFilter and verifies a Bearer
 *    JWT locally, attaching SCOPE_* authorities.
 *
 * NOTE: 401/403 responses for protected routes are produced by the entry point / access-denied
 * handler; controller-level [ai.cypherx.auth.web.ApiException]s flow through the Contract 2
 * [ai.cypherx.auth.web.GlobalExceptionHandler].
 */
@Configuration
class SecurityConfig {

    @Bean
    fun agentJwtAuthFilter(
        jwtMintService: JwtMintService,
        revocationChecker: RevocationChecker,
    ): AgentJwtAuthFilter = AgentJwtAuthFilter(jwtMintService, revocationChecker)

    /**
     * Self-protection rate-limit filter (WP03 Component 4). Inserted BEFORE [AgentJwtAuthFilter] so an
     * unauthenticated flood is capped before auth/DB/crypto work; it derives scope keys from the
     * (unverified) bearer claims + Kong headers + client IP itself. Limits come from
     * `auth.rate_limit_config`; behaviour is fail-open with an in-process backstop.
     */
    @Bean
    fun rateLimitFilter(
        rateLimitConfigRepository: RateLimitConfigRepository,
        rateLimitProperties: RateLimitProperties,
        redisProvider: ObjectProvider<StringRedisTemplate>,
        meterRegistryProvider: ObjectProvider<MeterRegistry>,
    ): RateLimitFilter =
        RateLimitFilter(rateLimitConfigRepository, rateLimitProperties, redisProvider, meterRegistryProvider)

    @Bean
    fun securityFilterChain(
        http: HttpSecurity,
        agentJwtAuthFilter: AgentJwtAuthFilter,
        rateLimitFilter: RateLimitFilter,
        contractAuthenticationEntryPoint: ContractAuthenticationEntryPoint,
        contractAccessDeniedHandler: ContractAccessDeniedHandler,
    ): SecurityFilterChain {
        http {
            csrf { disable() }
            cors { disable() }
            httpBasic { disable() }
            formLogin { disable() }
            sessionManagement { sessionCreationPolicy = SessionCreationPolicy.STATELESS }

            // Contract 1/2: a missing/invalid bearer on a protected route → 401 + Contract-2 envelope
            // (Spring's default is a 403 with an empty body); an authenticated-but-unauthorized
            // principal → 403 + envelope. These run in the filter chain, before the DispatcherServlet,
            // so GlobalExceptionHandler never sees them — hence the dedicated renderers.
            exceptionHandling {
                authenticationEntryPoint = contractAuthenticationEntryPoint
                accessDeniedHandler = contractAccessDeniedHandler
            }

            authorizeHttpRequests {
                // ── Public, unauthenticated surface ──────────────────────────────────────────
                authorize("/.well-known/**", permitAll)
                authorize("/metrics", permitAll)
                authorize("/livez", permitAll)
                authorize("/readyz", permitAll)
                authorize("/oauth/token", permitAll)
                authorize(HttpMethod.POST, "/v1/onboarding/signup", permitAll)
                authorize(HttpMethod.GET, "/v1/onboarding/verify", permitAll)
                authorize(HttpMethod.POST, "/v1/onboarding/resend", permitAll)
                authorize(HttpMethod.POST, "/v1/admin/bootstrap", permitAll)
                authorize(HttpMethod.POST, "/v1/agents/*/token", permitAll)
                authorize(HttpMethod.POST, "/v1/service-tokens", permitAll)
                // End-user auth (email/password + Google) — body-authenticates via password/OAuth code.
                authorize(HttpMethod.POST, "/v1/auth/register", permitAll)
                authorize(HttpMethod.POST, "/v1/auth/login", permitAll)
                // Session refresh/logout body-authenticate via the opaque refresh token (not a Bearer JWT).
                authorize(HttpMethod.POST, "/v1/auth/refresh", permitAll)
                authorize(HttpMethod.POST, "/v1/auth/logout", permitAll)
                authorize(HttpMethod.GET, "/v1/auth/oauth2/google", permitAll)
                authorize(HttpMethod.GET, "/v1/auth/oauth2/google/callback", permitAll)
                authorize(HttpMethod.POST, "/v1/auth/oauth2/google/callback", permitAll)

                // ── Everything else needs an authenticated principal ─────────────────────────
                authorize(anyRequest, authenticated)
            }

            addFilterBefore<UsernamePasswordAuthenticationFilter>(agentJwtAuthFilter)
            // Rate limiting runs EARLIER than auth (before the agent-JWT filter) so it caps
            // unauthenticated floods too — see RateLimitFilter's class doc for scope-key derivation.
            addFilterBefore(rateLimitFilter, AgentJwtAuthFilter::class.java)
        }
        return http.build()
    }
}
