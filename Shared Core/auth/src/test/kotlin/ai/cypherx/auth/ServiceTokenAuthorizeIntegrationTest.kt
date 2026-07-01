package ai.cypherx.auth

import ai.cypherx.auth.support.AbstractIntegrationTest
import ai.cypherx.auth.support.AuthFlows
import ai.cypherx.auth.support.PLATFORM_TENANT
import ai.cypherx.auth.support.TestHttp
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.http.HttpStatus

/**
 * Service tokens (Contract 12: service_acl-derived scopes, bootstrap-secret auth) and the
 * /authorize decision (RBAC, Contract 13 body-tenant anti-pattern, hash-chained audit write).
 */
class ServiceTokenAuthorizeIntegrationTest : AbstractIntegrationTest() {

    @LocalServerPort private var port: Int = 0
    private lateinit var http: TestHttp

    @BeforeEach
    fun setUp() {
        resetState()
        http = TestHttp("http://localhost:$port")
    }

    private fun serviceToken(service: String, secret: String, onBehalfOf: String? = null) =
        http.post(
            "/v1/service-tokens",
            body = buildMap {
                put("tenant_id", PLATFORM_TENANT.toString())
                if (onBehalfOf != null) put("on_behalf_of", onBehalfOf)
            },
            headers = http.headers {
                set("X-Service-Name", service)
                set("X-Service-Bootstrap-Secret", secret)
            },
        )

    @Test
    fun `service token derives scopes from service_acl and has aud star`() {
        val r = serviceToken("xagent", "test-xagent-secret")
        assertThat(r.statusCode).isEqualTo(HttpStatus.OK)
        assertThat(r.body!!["token"]).isNotNull()
        @Suppress("UNCHECKED_CAST")
        val aud = r.body!!["aud"] as List<String>
        assertThat(aud).contains("*")
        val claims = AuthFlows.decodeClaims(r.body!!["token"] as String)
        assertThat(claims["sub"]).isEqualTo("svc:xagent")
        @Suppress("UNCHECKED_CAST")
        val scopes = claims["scopes"] as List<String>
        assertThat(scopes).isNotEmpty()
    }

    @Test
    fun `wrong bootstrap secret is 401`() {
        val r = serviceToken("xagent", "WRONG-secret")
        assertThat(r.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
    }

    @Test
    fun `service with valid secret but no service_acl rows is 403`() {
        val r = serviceToken("noacl", "test-noacl-secret")
        assertThat(r.statusCode).isEqualTo(HttpStatus.FORBIDDEN)
    }

    /** Bootstrap admin, then create a worker agent (with [scopes]) and mint its JWT. Returns (workerId, workerJwt). */
    private fun worker(scopes: List<String>): Pair<String, String> {
        val admin = AuthFlows.bootstrap(http)
        val adminJwt = AuthFlows.mintToken(http, admin.agentId, admin.apiKey, PLATFORM_TENANT)
        val adminHeaders = http.headers {
            set("Authorization", "Bearer $adminJwt")
            set("X-Tenant-ID", PLATFORM_TENANT.toString())
        }
        val created = http.post(
            "/v1/agents",
            body = mapOf("name" to "az-worker", "version" to "1.0.0", "allowed_scopes" to scopes, "tenant_id" to PLATFORM_TENANT.toString()),
            headers = adminHeaders,
        )
        check(created.statusCode.is2xxSuccessful) { "create worker failed: ${created.statusCode} ${created.body}" }
        val workerId = created.body!!["agent_id"] as String
        val keyResp = http.post(
            "/v1/agents/$workerId/keys",
            body = mapOf("scopes" to scopes, "name" to "az-key"),
            headers = adminHeaders,
        )
        check(keyResp.statusCode.is2xxSuccessful) { "issue key failed: ${keyResp.statusCode} ${keyResp.body}" }
        val workerKey = keyResp.body!!["api_key"] as String
        return workerId to AuthFlows.mintToken(http, workerId, workerKey, PLATFORM_TENANT)
    }

    @Test
    fun `authorize allows a permitted action and writes an audit row`() {
        val (workerId, workerJwt) = worker(listOf("llm:invoke", "guardrails:check"))
        val svcJwt = serviceToken("xagent", "test-xagent-secret", workerId).body!!["token"] as String

        val before = superuserJdbc().queryForObject("SELECT count(*) FROM auth.audit_log", Long::class.java)!!
        val az = http.post(
            "/v1/authorize",
            body = mapOf("action" to "llm:invoke", "resource" to "model:default"),
            headers = http.headers {
                set("Authorization", "Bearer $svcJwt")
                set("X-Forwarded-Agent-JWT", workerJwt)
            },
        )
        assertThat(az.statusCode).isEqualTo(HttpStatus.OK)
        assertThat(az.body!!["allowed"]).isEqualTo(true)
        @Suppress("UNCHECKED_CAST")
        val policyIds = az.body!!["policy_ids"] as List<String>
        assertThat(policyIds).contains("default-allow-first-cycle")

        val after = superuserJdbc().queryForObject("SELECT count(*) FROM auth.audit_log", Long::class.java)!!
        assertThat(after).isGreaterThan(before)
    }

    @Test
    fun `authorize rejects tenant_id in the body (Contract 13 anti-pattern)`() {
        val (workerId, workerJwt) = worker(listOf("llm:invoke"))
        val svcJwt = serviceToken("xagent", "test-xagent-secret", workerId).body!!["token"] as String

        val az = http.post(
            "/v1/authorize",
            body = mapOf(
                "action" to "llm:invoke",
                "resource" to "model:default",
                "tenant_id" to PLATFORM_TENANT.toString(), // forbidden: identity must come from the JWT
            ),
            headers = http.headers {
                set("Authorization", "Bearer $svcJwt")
                set("X-Forwarded-Agent-JWT", workerJwt)
            },
        )
        assertThat(az.statusCode).isEqualTo(HttpStatus.BAD_REQUEST)
    }
}
