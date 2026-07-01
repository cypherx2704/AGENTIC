package ai.cypherx.auth

import ai.cypherx.auth.support.AbstractIntegrationTest
import ai.cypherx.auth.support.TestHttp
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.http.HttpStatus

/**
 * `POST /v1/admin/bootstrap` — one-time super-admin initialisation.
 *
 *  - happy path: 201, returns api_key ONCE + agent_id, platform:admin scope;
 *  - second call: 410 Gone (sentinel written);
 *  - bad/missing X-Bootstrap-Token: 401.
 */
class BootstrapIntegrationTest : AbstractIntegrationTest() {

    @LocalServerPort
    private var port: Int = 0

    private lateinit var http: TestHttp

    @BeforeEach
    fun setUp() {
        resetState()
        http = TestHttp("http://localhost:$port")
    }

    @Test
    fun `bootstrap happy path returns 201 with api_key once and platform admin scope`() {
        val resp = http.post(
            "/v1/admin/bootstrap",
            body = mapOf("name" to "super-admin"),
            headers = http.headers { set("X-Bootstrap-Token", "test-bootstrap-token") },
        )

        assertThat(resp.statusCode).isEqualTo(HttpStatus.CREATED)
        val body = resp.body!!
        assertThat(body["agent_id"]).isNotNull()
        assertThat(body["tenant_id"]).isEqualTo(PLATFORM_TENANT_AS_STRING)
        // Global SNAKE_CASE: BootstrapResponse.apiKey -> api_key, keyPrefix -> key_prefix.
        val apiKey = body["api_key"] as String
        assertThat(apiKey).startsWith("cx_test_")
        assertThat(body["key_prefix"]).isEqualTo(apiKey.take(8))

        @Suppress("UNCHECKED_CAST")
        val scopes = body["scopes"] as List<String>
        assertThat(scopes).containsExactly("platform:admin")
    }

    @Test
    fun `second bootstrap call returns 410 Gone`() {
        val first = http.post(
            "/v1/admin/bootstrap",
            body = emptyMap<String, Any>(),
            headers = http.headers { set("X-Bootstrap-Token", "test-bootstrap-token") },
        )
        assertThat(first.statusCode).isEqualTo(HttpStatus.CREATED)

        val second = http.post(
            "/v1/admin/bootstrap",
            body = emptyMap<String, Any>(),
            headers = http.headers { set("X-Bootstrap-Token", "test-bootstrap-token") },
        )
        assertThat(second.statusCode).isEqualTo(HttpStatus.GONE)
    }

    @Test
    fun `bad bootstrap token returns 401`() {
        val resp = http.post(
            "/v1/admin/bootstrap",
            body = emptyMap<String, Any>(),
            headers = http.headers { set("X-Bootstrap-Token", "wrong-token") },
        )
        assertThat(resp.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
    }

    @Test
    fun `missing bootstrap token returns 401`() {
        val resp = http.post(
            "/v1/admin/bootstrap",
            body = emptyMap<String, Any>(),
        )
        assertThat(resp.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
    }

    private companion object {
        const val PLATFORM_TENANT_AS_STRING = "00000000-0000-0000-0000-000000000001"
    }
}
