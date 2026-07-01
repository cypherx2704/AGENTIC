package ai.cypherx.auth

import ai.cypherx.auth.support.AbstractIntegrationTest
import ai.cypherx.auth.support.AuthFlows
import ai.cypherx.auth.support.PLATFORM_TENANT
import ai.cypherx.auth.support.TestHttp
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.http.HttpHeaders
import org.springframework.http.HttpStatus

/**
 * The authenticated agent spine: create agent (scope-gated), issue API key, mint agent JWT
 * (Contract 1 claims), and the token-exchange negative cases.
 */
class AgentTokenIntegrationTest : AbstractIntegrationTest() {

    @LocalServerPort private var port: Int = 0
    private lateinit var http: TestHttp

    @BeforeEach
    fun setUp() {
        resetState()
        http = TestHttp("http://localhost:$port")
    }

    private fun authHeaders(jwt: String): HttpHeaders = http.headers {
        set("Authorization", "Bearer $jwt")
        set("X-Tenant-ID", PLATFORM_TENANT.toString())
    }

    @Test
    fun `creating an agent without auth is rejected`() {
        val r = http.post(
            "/v1/agents",
            body = mapOf(
                "name" to "nope", "version" to "1.0.0",
                "allowed_scopes" to listOf("llm:invoke"), "tenant_id" to PLATFORM_TENANT.toString(),
            ),
            headers = http.headers { set("X-Tenant-ID", PLATFORM_TENANT.toString()) }, // no Authorization
        )
        assertThat(r.statusCode).isIn(HttpStatus.UNAUTHORIZED, HttpStatus.FORBIDDEN)
    }

    @Test
    fun `full lifecycle - create agent, issue key, mint worker JWT with Contract 1 claims`() {
        val adminJwt = AuthFlows.adminJwt(http)

        val created = http.post(
            "/v1/agents",
            body = mapOf(
                "name" to "worker", "version" to "1.0.0",
                "allowed_scopes" to listOf("llm:invoke", "guardrails:check"),
                "tenant_id" to PLATFORM_TENANT.toString(),
            ),
            headers = authHeaders(adminJwt),
        )
        assertThat(created.statusCode).isIn(HttpStatus.OK, HttpStatus.CREATED)
        val agentId = created.body!!["agent_id"] as String

        val keyResp = http.post(
            "/v1/agents/$agentId/keys",
            body = mapOf("scopes" to listOf("llm:invoke", "guardrails:check"), "name" to "worker-key"),
            headers = authHeaders(adminJwt),
        )
        assertThat(keyResp.statusCode).isIn(HttpStatus.OK, HttpStatus.CREATED)
        val rawKey = keyResp.body!!["api_key"] as String
        assertThat(rawKey).startsWith("cx_test_")
        assertThat(keyResp.body!!["key_prefix"]).isEqualTo(rawKey.take(8))

        val token = AuthFlows.mintToken(http, agentId, rawKey, PLATFORM_TENANT)
        val claims = AuthFlows.decodeClaims(token)
        assertThat(claims["iss"]).isEqualTo("http://localhost")
        assertThat(claims["aud"].toString()).contains("cypherx-platform")
        assertThat(claims["tenant_id"]).isEqualTo(PLATFORM_TENANT.toString())
        assertThat(claims["agent_id"]).isEqualTo(agentId)
        assertThat(claims["sub"]).isEqualTo(agentId)
        @Suppress("UNCHECKED_CAST")
        val scopes = claims["scopes"] as List<String>
        assertThat(scopes).contains("llm:invoke", "guardrails:check")
        val ttl = (claims["exp"] as Number).toLong() - (claims["iat"] as Number).toLong()
        assertThat(ttl).isLessThanOrEqualTo(3600)
    }

    @Test
    fun `token mint with an unknown api_key is 401`() {
        // Need an agent to address; bootstrap one then use a bogus key.
        val admin = AuthFlows.bootstrap(http)
        val r = http.post(
            "/v1/agents/${admin.agentId}/token",
            body = mapOf("api_key" to "cx_test_this-key-does-not-exist-000000000000000000"),
            headers = http.headers { set("X-Tenant-ID", PLATFORM_TENANT.toString()) },
        )
        assertThat(r.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
    }

    @Test
    fun `token mint without X-Tenant-ID is 401`() {
        val admin = AuthFlows.bootstrap(http)
        val r = http.post(
            "/v1/agents/${admin.agentId}/token",
            body = mapOf("api_key" to admin.apiKey), // no X-Tenant-ID header
        )
        assertThat(r.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
    }
}
