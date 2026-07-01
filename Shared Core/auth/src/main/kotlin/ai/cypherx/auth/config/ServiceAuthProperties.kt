package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Per-service bootstrap-secret configuration for `POST /v1/service-tokens` (Component 8b, first
 * cycle). Bound from the `cypherx.service-auth.*` tree and picked up by the
 * `@ConfigurationPropertiesScan` on [ai.cypherx.auth.AuthApplication].
 *
 * In cloud, each service's bootstrap secret is sourced from Doppler
 * (`service-auth/<service-name>/bootstrap_secret`) and injected as env vars that map onto
 * [bootstrapSecrets]. For LOCAL/dev, set them under `cypherx.service-auth.bootstrap-secrets` in
 * application-local.yaml, e.g.
 *
 *     cypherx:
 *       service-auth:
 *         bootstrap-secrets:
 *           xagent: "local-dev-xagent-secret"
 *           llms-gateway: "local-dev-llms-secret"
 *
 * The map key is the `X-Service-Name`; the value is the shared secret compared (constant-time)
 * against the `X-Service-Bootstrap-Secret` header. A service with no entry here cannot mint a
 * service token regardless of its service_acl rows.
 */
@ConfigurationProperties(prefix = "cypherx.service-auth")
data class ServiceAuthProperties(

    /** serviceName -> bootstrap secret. Empty in environments where SPIFFE replaces this (Phase 13). */
    val bootstrapSecrets: Map<String, String> = emptyMap(),
)
