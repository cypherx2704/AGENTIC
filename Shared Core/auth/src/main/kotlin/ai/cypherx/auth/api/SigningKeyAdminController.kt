package ai.cypherx.auth.api

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.domain.PLATFORM_TENANT_ID
import ai.cypherx.auth.service.AuditService
import ai.cypherx.auth.service.CallerContext
import ai.cypherx.auth.service.RevocationService
import ai.cypherx.auth.signing.SigningKey
import ai.cypherx.auth.signing.SigningKeyService
import ai.cypherx.auth.web.ApiException
import com.fasterxml.jackson.annotation.JsonInclude
import com.fasterxml.jackson.annotation.JsonProperty
import org.slf4j.LoggerFactory
import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestHeader
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController
import java.nio.file.Files
import java.nio.file.Path
import java.time.Instant

/**
 * Platform-admin signing-key rotation (Component 3, WP03).
 *
 *   POST /v1/admin/signing-keys/rotate            standard rotation (old key stays VERIFYING)
 *   POST /v1/admin/signing-keys/emergency-rotate  compromise rotation (old key POISONED)
 *   GET  /v1/admin/signing-keys                   list keys for operability
 *
 * Auth: every route requires the `platform:admin` scope (enforced in-handler via
 * [CallerContext.requireAnyScope] — method-level `@PreAuthorize` is not enabled service-wide; see
 * [ai.cypherx.auth.api.TenantAdminController]). Signing keys are PLATFORM-scoped (no tenant), so
 * audit rows are written under [PLATFORM_TENANT_ID].
 *
 * `emergency-rotate` carries a SECOND, out-of-band gate on top of the admin scope: the request must
 * present an `X-Emergency-Token` header whose value matches the contents of the file at
 * [AuthProperties.emergencyRotateTokenFile] (read fresh at request time). This double gate (a
 * compromised admin JWT is not enough — you also need the on-disk secret) reflects how destructive
 * an emergency rotation is: it poisons the old signing kid, instantly rejecting every token that key
 * ever signed.
 */
@RestController
@RequestMapping("/v1/admin/signing-keys")
class SigningKeyAdminController(
    private val signingKeyService: SigningKeyService,
    private val revocationService: RevocationService,
    private val auditService: AuditService,
    private val callerContext: CallerContext,
    private val props: AuthProperties,
) {

    /** Standard rotation: new signing key; previous key stays VERIFYING for graceful in-flight validation. */
    @PostMapping("/rotate")
    fun rotate(): RotateResponse {
        val caller = callerContext.requireAnyScope(SCOPE_PLATFORM_ADMIN)
        val newKid = signingKeyService.rotate()
        auditService.record(
            eventType = "signing_key.rotated",
            tenantId = PLATFORM_TENANT_ID,
            agentId = caller.agentId,
            action = "signing_key:rotate",
            resource = "kid:$newKid",
            decision = "allow",
        )
        log.info("standard signing-key rotation by {} -> new kid={}", caller.subject, newKid)
        return RotateResponse(kid = newKid.toString(), status = "signing")
    }

    /**
     * Emergency rotation: requires the admin scope AND a valid out-of-band emergency token, then
     * rotates and POISONS the old kid so every token it signed is rejected immediately at every
     * verifier. Use only on suspected key compromise.
     */
    @PostMapping("/emergency-rotate")
    fun emergencyRotate(
        @RequestHeader(name = HEADER_EMERGENCY_TOKEN, required = false) emergencyToken: String?,
    ): EmergencyRotateResponse {
        val caller = callerContext.requireAnyScope(SCOPE_PLATFORM_ADMIN)
        verifyEmergencyToken(emergencyToken)

        val result = signingKeyService.emergencyRotate()
        result.oldKid?.let { revocationService.poisonKid(it.toString()) }

        auditService.record(
            eventType = "signing_key.emergency_rotated",
            tenantId = PLATFORM_TENANT_ID,
            agentId = caller.agentId,
            action = "signing_key:emergency-rotate",
            resource = "kid:${result.newKid}",
            decision = "allow",
        )
        log.warn(
            "EMERGENCY signing-key rotation by {} -> new kid={}, poisoned old kid={}",
            caller.subject, result.newKid, result.oldKid,
        )
        return EmergencyRotateResponse(
            kid = result.newKid.toString(),
            status = "signing",
            poisonedKid = result.oldKid?.toString(),
        )
    }

    /** Operability listing of every key (signing / verifying / retired) and its lifecycle timestamps. */
    @GetMapping
    fun list(): ResponseEntity<KeyListResponse> {
        callerContext.requireAnyScope(SCOPE_PLATFORM_ADMIN)
        val keys = signingKeyService.listAll().map(::toView)
        return ResponseEntity.ok(KeyListResponse(keys = keys))
    }

    // ── Emergency-token gate ────────────────────────────────────────────────────────────────

    /**
     * Read the gate file fresh and constant-time-compare it to the presented header. Absent/empty
     * file, missing/blank header, or mismatch -> 403 (the rotation never runs). The file path is
     * env-configurable ([AuthProperties.emergencyRotateTokenFile]) — never hardcoded.
     */
    private fun verifyEmergencyToken(presented: String?) {
        val expected = readEmergencyToken()
        if (expected.isNullOrBlank()) {
            log.warn("emergency-rotate denied: gate file {} is absent or empty", props.emergencyRotateTokenFile)
            throw ApiException.forbidden("Emergency rotation is not enabled (gate file missing or empty)")
        }
        if (presented.isNullOrBlank() || !constantTimeEquals(presented.trim(), expected)) {
            throw ApiException.forbidden("Invalid or missing emergency token")
        }
    }

    private fun readEmergencyToken(): String? = try {
        val path = Path.of(props.emergencyRotateTokenFile)
        if (Files.isReadable(path)) Files.readString(path).trim() else null
    } catch (ex: Exception) {
        log.warn("emergency-rotate gate file {} unreadable: {}", props.emergencyRotateTokenFile, ex.message)
        null
    }

    /** Length-aware constant-time comparison to avoid leaking the token via timing. */
    private fun constantTimeEquals(a: String, b: String): Boolean {
        val ab = a.toByteArray(Charsets.UTF_8)
        val bb = b.toByteArray(Charsets.UTF_8)
        var diff = ab.size xor bb.size
        for (i in ab.indices) {
            diff = diff or (ab[i].toInt() xor bb[i % bb.size.coerceAtLeast(1)].toInt())
        }
        return diff == 0
    }

    // ── view mapping ────────────────────────────────────────────────────────────────────────

    private fun toView(key: SigningKey): KeyView = KeyView(
        kid = key.kid.toString(),
        status = key.status.value,
        createdAt = key.createdAt,
        promotedAt = key.promotedAt,
        retiredAt = key.retiredAt,
    )

    // ── responses ───────────────────────────────────────────────────────────────────────────

    data class RotateResponse(
        @JsonProperty("kid") val kid: String,
        @JsonProperty("status") val status: String,
    )

    @JsonInclude(JsonInclude.Include.NON_NULL)
    data class EmergencyRotateResponse(
        @JsonProperty("kid") val kid: String,
        @JsonProperty("status") val status: String,
        /** The old, now-poisoned kid (tokens it signed are rejected immediately). */
        @JsonProperty("poisoned_kid") val poisonedKid: String?,
    )

    data class KeyListResponse(
        @JsonProperty("keys") val keys: List<KeyView>,
    )

    @JsonInclude(JsonInclude.Include.NON_NULL)
    data class KeyView(
        @JsonProperty("kid") val kid: String,
        @JsonProperty("status") val status: String,
        @JsonProperty("created_at") val createdAt: Instant,
        @JsonProperty("promoted_at") val promotedAt: Instant?,
        @JsonProperty("retired_at") val retiredAt: Instant?,
    )

    private companion object {
        const val SCOPE_PLATFORM_ADMIN = "platform:admin"
        const val HEADER_EMERGENCY_TOKEN = "X-Emergency-Token"
        val log = LoggerFactory.getLogger(SigningKeyAdminController::class.java)
    }
}
