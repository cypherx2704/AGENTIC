package ai.cypherx.auth.web

import ai.cypherx.auth.config.AuthProperties
import ai.cypherx.auth.crypto.KeyEncryptor
import org.slf4j.LoggerFactory
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController
import java.time.Instant

/**
 * Contract 7 health endpoints.
 *
 *  - `GET /livez`  — liveness. Process-only; **NEVER** touches DB/Kafka/Valkey. 200 always while
 *    the JVM can serve. Body: `{ status, version, uptime_seconds }`.
 *  - `GET /readyz` — readiness. Checks required downstreams (DB `SELECT 1`) + encryptor readiness.
 *    200 `{ ready:true, checks{...} }` when all pass, else 503 `{ ready:false, checks{...} }`.
 *
 * Both endpoints are permit-all in [ai.cypherx.auth.config.SecurityConfig] and mapped at the
 * origin root (no `/v1`). They are deliberately NOT Spring-Actuator endpoints so we control the
 * exact Contract 7 body shape.
 */
@RestController
class HealthController(
    private val jdbc: JdbcTemplate,
    private val encryptor: KeyEncryptor,
    private val props: AuthProperties,
) {

    private val startedAt: Instant = Instant.now()

    /** Liveness — process alive; no downstream checks. */
    @GetMapping("/livez")
    fun livez(): ResponseEntity<Map<String, Any>> =
        ResponseEntity.ok(
            mapOf(
                "status" to "ok",
                "version" to props.version,
                "uptime_seconds" to (Instant.now().epochSecond - startedAt.epochSecond),
            ),
        )

    /** Readiness — DB + encryptor must be healthy to serve traffic. */
    @GetMapping("/readyz")
    fun readyz(): ResponseEntity<Map<String, Any>> {
        val checks = linkedMapOf<String, String>()

        checks["database"] = try {
            jdbc.queryForObject("SELECT 1", Int::class.java)
            "ok"
        } catch (ex: Exception) {
            log.warn("readyz: database check failed: {}", ex.message)
            "failed"
        }

        checks["encryptor"] = try {
            if (encryptor.isReady()) "ok" else "failed"
        } catch (ex: Exception) {
            log.warn("readyz: encryptor check failed: {}", ex.message)
            "failed"
        }

        val ready = checks.values.all { it == "ok" }
        val status = if (ready) HttpStatus.OK else HttpStatus.SERVICE_UNAVAILABLE
        return ResponseEntity.status(status).body(mapOf("ready" to ready, "checks" to checks))
    }

    private companion object {
        val log = LoggerFactory.getLogger(HealthController::class.java)
    }
}
