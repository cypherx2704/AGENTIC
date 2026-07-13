package ai.cypherx.auth

import ai.cypherx.auth.support.AbstractIntegrationTest
import ai.cypherx.auth.support.AuthFlows
import ai.cypherx.auth.support.TestHttp
import org.assertj.core.api.Assertions.assertThat
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.http.HttpStatus
import java.util.UUID

/**
 * End-user Console session lifecycle: register → login (mints a <=1h access JWT + a refresh token)
 * → refresh (silent renewal — a fresh <=1h access JWT for the same session) → logout (revokes it).
 *
 * This is the fix for the "session expired mid-work, everything lost" bug: the access token stays
 * <=1h (Contract 1) while the BFF-held refresh token lets an active session be renewed instead of
 * hard-expiring. Drives the real HTTP surface (RANDOM_PORT) like the other integration suites.
 */
class UserSessionRefreshIntegrationTest : AbstractIntegrationTest() {

    @LocalServerPort private var port: Int = 0
    private lateinit var http: TestHttp

    @BeforeEach
    fun setUp() {
        resetState()
        http = TestHttp("http://localhost:$port")
    }

    /** Register a fresh user (unique email) and log them in; returns the login response body. */
    private fun registerAndLogin(): Map<*, *> {
        val email = "refresh-it-${UUID.randomUUID()}@example.com"
        val password = "s3cret-password"
        val reg = http.post(
            "/v1/auth/register",
            body = mapOf("email" to email, "password" to password, "tenant_name" to "refresh-it"),
        )
        check(reg.statusCode == HttpStatus.CREATED) { "register failed: ${reg.statusCode} ${reg.body}" }

        val login = http.post("/v1/auth/login", body = mapOf("email" to email, "password" to password))
        check(login.statusCode == HttpStatus.OK) { "login failed: ${login.statusCode} ${login.body}" }
        return login.body!!
    }

    @Test
    fun `login issues an access token plus a refresh token, access TTL is capped at 1h`() {
        val body = registerAndLogin()

        val token = body["token"] as String
        val refreshToken = body["refresh_token"] as String
        assertThat(refreshToken).isNotBlank()
        assertThat(refreshToken).contains(".") // "<session_id>.<secret>"
        assertThat((body["expires_in"] as Number).toLong()).isGreaterThan(0).isLessThanOrEqualTo(3600)
        assertThat((body["refresh_expires_in"] as Number).toLong()).isGreaterThan(3600) // longer-lived than access

        val claims = AuthFlows.decodeClaims(token)
        val ttl = (claims["exp"] as Number).toLong() - (claims["iat"] as Number).toLong()
        assertThat(ttl).isLessThanOrEqualTo(3600)
        assertThat(claims["user_id"]).isEqualTo(body["user_id"])
        assertThat(claims["tenant_id"]).isEqualTo(body["tenant_id"])
    }

    @Test
    fun `refresh returns a fresh access token for the same session and echoes the refresh token`() {
        val login = registerAndLogin()
        val refreshToken = login["refresh_token"] as String

        val r = http.post("/v1/auth/refresh", body = mapOf("refresh_token" to refreshToken))
        assertThat(r.statusCode).isEqualTo(HttpStatus.OK)
        val body = r.body!!

        // Same identity, same session (non-rotating refresh token is echoed back unchanged).
        assertThat(body["user_id"]).isEqualTo(login["user_id"])
        assertThat(body["tenant_id"]).isEqualTo(login["tenant_id"])
        assertThat(body["agent_id"]).isEqualTo(login["agent_id"])
        assertThat(body["refresh_token"]).isEqualTo(refreshToken)

        val claims = AuthFlows.decodeClaims(body["token"] as String)
        val ttl = (claims["exp"] as Number).toLong() - (claims["iat"] as Number).toLong()
        assertThat(ttl).isLessThanOrEqualTo(3600)
    }

    @Test
    fun `refresh with a malformed or unknown token is 401 with a Contract-2 envelope`() {
        val garbage = http.post("/v1/auth/refresh", body = mapOf("refresh_token" to "not-a-real-token"))
        assertThat(garbage.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)

        val unknown = http.post(
            "/v1/auth/refresh",
            body = mapOf("refresh_token" to "${UUID.randomUUID()}.deadbeefdeadbeefdeadbeefdeadbeef"),
        )
        assertThat(unknown.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
        @Suppress("UNCHECKED_CAST")
        val error = unknown.body!!["error"] as Map<String, Any?>
        assertThat(error["code"]).isEqualTo("INVALID_REFRESH_TOKEN")
    }

    @Test
    fun `refresh with a valid session id but wrong secret is 401`() {
        val login = registerAndLogin()
        val refreshToken = login["refresh_token"] as String
        val sessionId = refreshToken.substringBefore(".")

        val r = http.post(
            "/v1/auth/refresh",
            body = mapOf("refresh_token" to "$sessionId.wrong-secret-wrong-secret-wrong"),
        )
        assertThat(r.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
    }

    @Test
    fun `logout revokes the session so a later refresh is rejected`() {
        val login = registerAndLogin()
        val refreshToken = login["refresh_token"] as String

        // Sanity: it refreshes before logout.
        assertThat(http.post("/v1/auth/refresh", body = mapOf("refresh_token" to refreshToken)).statusCode)
            .isEqualTo(HttpStatus.OK)

        val logout = http.post("/v1/auth/logout", body = mapOf("refresh_token" to refreshToken))
        assertThat(logout.statusCode).isEqualTo(HttpStatus.NO_CONTENT)

        val afterLogout = http.post("/v1/auth/refresh", body = mapOf("refresh_token" to refreshToken))
        assertThat(afterLogout.statusCode).isEqualTo(HttpStatus.UNAUTHORIZED)
    }

    @Test
    fun `logout is idempotent for a missing or unknown token`() {
        assertThat(http.post("/v1/auth/logout", body = emptyMap<String, Any>()).statusCode)
            .isEqualTo(HttpStatus.NO_CONTENT)
        assertThat(
            http.post("/v1/auth/logout", body = mapOf("refresh_token" to "${UUID.randomUUID()}.whatever")).statusCode,
        ).isEqualTo(HttpStatus.NO_CONTENT)
    }
}
