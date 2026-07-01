package ai.cypherx.auth.support

import org.springframework.http.HttpEntity
import org.springframework.http.HttpHeaders
import org.springframework.http.HttpMethod
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.web.client.RestTemplate
import java.util.UUID

/** The seeded platform tenant (auth's own admin agents live here). */
val PLATFORM_TENANT: UUID = UUID.fromString("00000000-0000-0000-0000-000000000001")

/** The seeded integration-test tenant (a distinct second tenant). */
val INTEGRATION_TEST_TENANT: UUID = UUID.fromString("00000000-0000-0000-0000-0000000000ff")

/**
 * Minimal HTTP helper around a [RestTemplate] that does NOT throw on 4xx/5xx (so tests can assert
 * the status + Contract 2 error envelope). All bodies are JSON; the server uses global SNAKE_CASE
 * naming, so request maps use snake_case keys.
 */
class TestHttp(private val baseUrl: String) {

    private val rest: RestTemplate = RestTemplate(
        // Use java.net.http.HttpClient (not the legacy HttpURLConnection): the latter treats any 401
        // as an auth challenge and throws HttpRetryException on a POST ("cannot retry due to server
        // authentication, in streaming mode"), so we could not assert auth-failure responses.
        // JdkClientHttpRequestFactory returns the 401/403 as a normal response.
        org.springframework.http.client.JdkClientHttpRequestFactory(),
    ).apply {
        errorHandler = object : org.springframework.web.client.ResponseErrorHandler {
            override fun hasError(response: org.springframework.http.client.ClientHttpResponse) = false
            override fun handleError(response: org.springframework.http.client.ClientHttpResponse) {}
        }
    }

    fun headers(block: HttpHeaders.() -> Unit = {}): HttpHeaders =
        HttpHeaders().apply {
            contentType = MediaType.APPLICATION_JSON
            accept = listOf(MediaType.APPLICATION_JSON)
            block()
        }

    @Suppress("UNCHECKED_CAST")
    fun exchange(
        method: HttpMethod,
        path: String,
        body: Any? = null,
        headers: HttpHeaders = headers(),
    ): ResponseEntity<Map<*, *>> {
        val entity = HttpEntity(body, headers)
        return rest.exchange("$baseUrl$path", method, entity, Map::class.java)
    }

    fun post(path: String, body: Any? = null, headers: HttpHeaders = headers()) =
        exchange(HttpMethod.POST, path, body, headers)

    fun get(path: String, headers: HttpHeaders = headers()) =
        exchange(HttpMethod.GET, path, null, headers)

    fun delete(path: String, headers: HttpHeaders = headers()) =
        exchange(HttpMethod.DELETE, path, null, headers)

    /** GET returning the raw body as a typed map (for well-known docs etc.). */
    fun getMap(path: String): ResponseEntity<Map<*, *>> = get(path)
}
