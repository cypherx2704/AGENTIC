package ai.cypherx.auth.support

import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.http.HttpStatus
import java.util.Base64
import java.util.UUID

/**
 * Reusable auth flows for integration tests: run the one-time bootstrap, exchange an api_key for a
 * JWT, and decode JWT claims. Keeps each test focused on its assertions.
 */
object AuthFlows {

    const val BOOTSTRAP_TOKEN = "test-bootstrap-token"
    private val mapper = ObjectMapper()

    /** The created super-admin agent + its initial api_key (returned once by bootstrap). */
    data class Admin(val agentId: String, val apiKey: String)

    /** Run POST /v1/admin/bootstrap; returns the super-admin agent_id + its initial api_key. */
    fun bootstrap(http: TestHttp, name: String = "it-admin"): Admin {
        val r = http.post(
            "/v1/admin/bootstrap",
            body = mapOf("name" to name),
            headers = http.headers { set("X-Bootstrap-Token", BOOTSTRAP_TOKEN) },
        )
        check(r.statusCode == HttpStatus.CREATED) { "bootstrap failed: ${r.statusCode} ${r.body}" }
        val b = r.body!!
        return Admin(b["agent_id"] as String, b["api_key"] as String)
    }

    /** Exchange an api_key for an agent JWT. Passes X-Tenant-ID (as Kong would, from the JWT). */
    fun mintToken(http: TestHttp, agentId: String, apiKey: String, tenant: UUID): String {
        val r = http.post(
            "/v1/agents/$agentId/token",
            body = mapOf("api_key" to apiKey),
            headers = http.headers { set("X-Tenant-ID", tenant.toString()) },
        )
        check(r.statusCode == HttpStatus.OK) { "token mint failed: ${r.statusCode} ${r.body}" }
        return r.body!!["token"] as String
    }

    /** Convenience: bootstrap + mint the super-admin's platform:admin JWT. */
    fun adminJwt(http: TestHttp): String {
        val a = bootstrap(http)
        return mintToken(http, a.agentId, a.apiKey, PLATFORM_TENANT)
    }

    /** Decode (without verifying) the JWT payload claims. */
    fun decodeClaims(jwt: String): Map<String, Any?> {
        val payload = jwt.split(".")[1]
        val json = String(Base64.getUrlDecoder().decode(payload))
        @Suppress("UNCHECKED_CAST")
        return mapper.readValue(json, Map::class.java) as Map<String, Any?>
    }
}
