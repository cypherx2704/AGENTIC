package ai.cypherx.auth.domain

import java.util.UUID

/**
 * Well-known constants and enums shared across the auth-service.
 *
 * The string values of every enum here are the EXACT tokens persisted in PostgreSQL
 * `VARCHAR` status/source columns — do not rename a value without a migration.
 */

/** The SYSTEM-USER sentinel: created_by for bootstrap / manual-seed agents (no px0 user). */
val SYSTEM_USER_ID: UUID = UUID.fromString("00000000-0000-0000-0000-000000000000")

/** Well-known platform tenant (Auth's own admin agents live here). */
val PLATFORM_TENANT_ID: UUID = UUID.fromString("00000000-0000-0000-0000-000000000001")

/** Well-known integration-test tenant (rejected in prod via ENVIRONMENT gate). */
val INTEGRATION_TEST_TENANT_ID: UUID = UUID.fromString("00000000-0000-0000-0000-0000000000ff")

/** auth.agents.status */
enum class AgentStatus(val value: String) {
    ACTIVE("active"),
    INACTIVE("inactive"),
    SUSPENDED("suspended"),
    QUARANTINED("quarantined");

    companion object {
        fun from(value: String): AgentStatus =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown agent status: $value")
    }
}

/** auth.tenants.status */
enum class TenantStatus(val value: String) {
    ACTIVE("active"),
    PENDING_VERIFICATION("pending_verification"),
    SUSPENDED("suspended"),
    PENDING_DELETION("pending_deletion"),
    DELETED("deleted");

    companion object {
        fun from(value: String): TenantStatus =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown tenant status: $value")
    }
}

/** auth.tenants.source (Contract 13 source enum) */
enum class TenantSource(val value: String) {
    PX0_BRIDGE("px0-bridge"),
    EXTERNAL_ADMIN("external-admin"),
    SELF_SERVE_SIGNUP("self-serve-signup"),
    SSO_JIT("sso-jit"),
    MANUAL_SEED("manual-seed");

    companion object {
        fun from(value: String): TenantSource =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown tenant source: $value")
    }
}

/** auth.signing_keys.status — signing | verifying | retired (NOT active/retiring/retired). */
enum class SigningKeyStatus(val value: String) {
    /** The single key new tokens are minted with (partial-unique: exactly one). */
    SIGNING("signing"),

    /** A previously-signing key kept in JWKS to verify in-flight tokens. */
    VERIFYING("verifying"),

    /** Removed from JWKS; kept for audit. */
    RETIRED("retired");

    companion object {
        fun from(value: String): SigningKeyStatus =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown signing key status: $value")
    }
}

/** auth.api_keys.status */
enum class ApiKeyStatus(val value: String) {
    ACTIVE("active"),
    REVOKED("revoked"),
    EXPIRED("expired"),
    ROTATING("rotating");

    companion object {
        fun from(value: String): ApiKeyStatus =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown api key status: $value")
    }
}

/** auth.service_clients.status */
enum class ServiceClientStatus(val value: String) {
    ACTIVE("active"),
    ROTATING("rotating"),
    REVOKED("revoked");

    companion object {
        fun from(value: String): ServiceClientStatus =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown service client status: $value")
    }
}

/** auth.revoked_tokens.reason */
enum class RevocationReason(val value: String) {
    COMPROMISED("compromised"),
    ROTATED("rotated"),
    DEACTIVATED("deactivated"),
    POLICY_VIOLATION("policy_violation"),
    ADMIN_ACTION("admin_action");

    companion object {
        fun from(value: String): RevocationReason =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown revocation reason: $value")
    }
}

/** auth.behavior_policies.enforcement */
enum class BehaviorEnforcement(val value: String) {
    BLOCK("block"),
    QUARANTINE("quarantine"),
    ALERT("alert");

    companion object {
        fun from(value: String): BehaviorEnforcement =
            entries.firstOrNull { it.value == value }
                ?: throw IllegalArgumentException("unknown enforcement: $value")
    }
}

/** Which envelope-encryptor backs signing-key private material. */
enum class KeyEncryptorKind(val value: String) {
    LOCAL("local"),
    KMS("kms");

    companion object {
        fun from(value: String): KeyEncryptorKind =
            entries.firstOrNull { it.value.equals(value, ignoreCase = true) }
                ?: throw IllegalArgumentException("unknown key-encryptor kind: $value")
    }
}
