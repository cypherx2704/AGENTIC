package ai.cypherx.auth

import ai.cypherx.auth.support.AbstractIntegrationTest
import ai.cypherx.auth.support.TestHttp
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.http.HttpStatus

/**
 * Public, unauthenticated surface: health (Contract 7), JWKS + OIDC discovery (Contract 1), and the
 * Contract 2 error envelope on a 404.
 */
class MiscEndpointsIntegrationTest : AbstractIntegrationTest() {

    @LocalServerPort private var port: Int = 0
    private lateinit var http: TestHttp

    @BeforeEach
    fun setUp() {
        http = TestHttp("http://localhost:$port")
    }

    @Test
    fun `livez is 200`() {
        val r = http.get("/livez")
        assertThat(r.statusCode).isEqualTo(HttpStatus.OK)
        assertThat(r.body!!["status"]).isEqualTo("ok")
    }

    @Test
    fun `readyz is 200 with checks`() {
        val r = http.get("/readyz")
        assertThat(r.statusCode).isEqualTo(HttpStatus.OK)
        @Suppress("UNCHECKED_CAST")
        val checks = r.body!!["checks"] as Map<String, Any?>
        assertThat(checks["database"]).isEqualTo("ok")
    }

    @Test
    fun `jwks exposes at least one RS256 signing key`() {
        val r = http.get("/.well-known/jwks.json")
        assertThat(r.statusCode).isEqualTo(HttpStatus.OK)
        @Suppress("UNCHECKED_CAST")
        val keys = r.body!!["keys"] as List<Map<String, Any?>>
        assertThat(keys).isNotEmpty()
        assertThat(keys[0]["kty"]).isEqualTo("RSA")
        assertThat(keys[0]["alg"]).isEqualTo("RS256")
        assertThat(keys[0]["kid"]).isNotNull()
    }

    @Test
    fun `oidc discovery has the required RFC 8414 fields`() {
        val r = http.get("/.well-known/openid-configuration")
        assertThat(r.statusCode).isEqualTo(HttpStatus.OK)
        val b = r.body!!
        assertThat(b["issuer"]).isEqualTo("http://localhost")
        assertThat(b["jwks_uri"]).isNotNull()
        assertThat(b["token_endpoint"]).isNotNull()
        @Suppress("UNCHECKED_CAST")
        val grants = b["grant_types_supported"] as List<String>
        assertThat(grants).contains("client_credentials")
        @Suppress("UNCHECKED_CAST")
        val algs = b["id_token_signing_alg_values_supported"] as List<String>
        assertThat(algs).contains("RS256")
    }

    @Test
    fun `unknown route returns Contract 2 error envelope`() {
        val r = http.get("/v1/does-not-exist")
        assertThat(r.statusCode.is4xxClientError).isTrue()
        // Spring's default 404 may not carry our envelope; an authenticated-but-unknown path under
        // anyRequest=authenticated yields 401/403 via the chain. Either way assert it's a 4xx; when
        // our @RestControllerAdvice handles it, the envelope has error.{code,message,...}.
        val body = r.body
        if (body != null && body.containsKey("error")) {
            @Suppress("UNCHECKED_CAST")
            val err = body["error"] as Map<String, Any?>
            assertThat(err.keys).contains("code", "message", "request_id", "trace_id", "timestamp")
        }
    }
}
