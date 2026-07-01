package ai.cypherx.auth.config

import org.springframework.boot.context.properties.ConfigurationProperties

/**
 * Strongly-typed binding of the `cypherx.auth.*` configuration tree
 * (see src/main/resources/application.yaml).
 *
 * Bound automatically by @ConfigurationPropertiesScan on [ai.cypherx.auth.AuthApplication].
 * Inject this bean by constructor wherever issuer/audience/TTL/crypto config is needed.
 *
 * Notes:
 *  - [issuerUrl] and [platformAudience] are deployment-configurable (Contract 1) — never hardcode.
 *  - [keyEncryptor] selects the signing-key envelope-encryptor bean: "local" (AES, dev) or "kms".
 *  - There is NO JWT private key here — signing keys live in auth.signing_keys, envelope-encrypted.
 */
@ConfigurationProperties(prefix = "cypherx.auth")
data class AuthProperties(

    /** Token `iss`. Verifiers match against this (Contract 1). e.g. https://auth.cypherx.ai */
    val issuerUrl: String,

    /** Required member of every token's `aud` array (Contract 1). e.g. cypherx-platform */
    val platformAudience: String,

    /** Disambiguates tokens when a verifier trusts more than one issuer (claim `deployment_id`). */
    val deploymentId: String,

    /** Deployment environment: local | dev | staging | prod. Gates the integration-test tenant. */
    val environment: String,

    /** Agent token TTL. MUST be <= 3600s (Contract 1). */
    val agentTokenTtlSeconds: Long = 3600,

    /** Internal service token TTL = 300s (Contract 12). */
    val serviceTokenTtlSeconds: Long = 300,

    /** Tolerated clock skew (seconds) when verifying exp/nbf. */
    val clockSkewSeconds: Long = 60,

    /** Signing-key envelope encryptor: "local" (AES) or "kms". Selects the KeyEncryptor bean. */
    val keyEncryptor: String = "local",

    /** AWS KMS Customer-Managed Key ARN (cloud only; required when keyEncryptor == "kms"). */
    val kmsSigningCmkArn: String? = null,

    /** Base64 of a 32-byte AES master key (LOCAL/dev only; required when keyEncryptor == "local"). */
    val localMasterKeyB64: String? = null,

    /** One-time super-admin bootstrap token. Rejected after the bootstrap_state sentinel exists. */
    val bootstrapToken: String? = null,

    /** Signing-key rotation cadence in days (default 90; Contract 1). */
    val signingKeyRotationDays: Long = 90,

    /**
     * How long a demoted key stays `verifying` (still in JWKS) before the retirement job moves it to
     * `retired` and drops it from JWKS. MUST comfortably exceed the longest in-flight token lifetime
     * ([agentTokenTtlSeconds], <=3600s = 1h) plus [clockSkewSeconds], so a token signed by the
     * just-demoted key still validates until it has naturally expired. Default 48h gives ample slack
     * over the 1h max agent-token TTL. (Standard rotation only — emergency rotation POISONS the old
     * kid immediately instead of waiting out this window.)
     */
    val verifyingKeyRetentionHours: Long = 48,

    /**
     * Fixed-delay cadence (ms) of the [ai.cypherx.auth.signing.SigningKeyRetirementJob] sweep that
     * retires `verifying` keys older than [verifyingKeyRetentionHours]. Default 1h — retirement is
     * not time-critical (the retention window already guards correctness). Env-overridable.
     */
    val verifyingKeyRetirementSweepMs: Long = 3_600_000,

    /**
     * Filesystem path to the out-of-band emergency-rotation gate file. The
     * `POST /v1/admin/signing-keys/emergency-rotate` endpoint requires the request's
     * `X-Emergency-Token` header to byte-match the trimmed contents of this file (read at request
     * time). An absent/empty file => 403, so a compromised admin JWT alone cannot trigger an
     * emergency rotation. Default points at a mounted secret path; always env-overridable.
     */
    val emergencyRotateTokenFile: String = "/etc/cypherx/auth/emergency-rotate-token",

    /**
     * Tenant soft-delete grace window in days (Contract 13: 30-day grace before hard-delete).
     * Drives the `grace_until` carried by `cypherx.tenant.pending_deletion`.
     */
    val tenantDeletionGraceDays: Long = 30,

    /** Service version surfaced on /livez and in OIDC docs. */
    val version: String = "0.1.0",

    /**
     * How long a HIL approval request (Phase 6) stays `pending` before it auto-expires. Should be
     * comfortably below the agent task timeout so a waiting agent gets a definitive answer. Default
     * 10 minutes.
     */
    val hilApprovalTtlSeconds: Long = 600,
)
