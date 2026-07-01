package ai.cypherx.auth.web

import com.fasterxml.jackson.databind.ObjectMapper
import jakarta.servlet.http.HttpServletRequest
import jakarta.servlet.http.HttpServletResponse
import org.slf4j.LoggerFactory
import org.slf4j.MDC
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.security.access.AccessDeniedException
import org.springframework.security.core.AuthenticationException
import org.springframework.security.web.AuthenticationEntryPoint
import org.springframework.security.web.access.AccessDeniedHandler
import org.springframework.stereotype.Component
import java.time.Instant
import java.util.UUID

/**
 * Spring-Security error renderers that emit the Contract-2 error envelope for the two cases that
 * never reach [GlobalExceptionHandler] (the filter chain rejects the request before the
 * DispatcherServlet, so the @RestControllerAdvice cannot see it):
 *
 *  - [ContractAuthenticationEntryPoint]  → **401 UNAUTHORIZED** for missing/invalid credentials on a
 *    protected route. Spring's default would otherwise return a 403 with an EMPTY body.
 *  - [ContractAccessDeniedHandler]       → **403 FORBIDDEN** for an authenticated principal that lacks
 *    the required authority. Spring's default returns a 403 with an EMPTY body.
 *
 * Both render exactly the Contract-2 shape
 *
 *     { "error": { "code", "message", "request_id", "trace_id", "timestamp" } }
 *
 * pulling request_id / trace_id from the MDC ([TraceContextFilter]) — byte-compatible with the
 * envelope written by [AgentJwtAuthFilter] and [GlobalExceptionHandler].
 */
private val SECURITY_ENVELOPE_MAPPER = ObjectMapper()

/** Render the Contract-2 envelope directly to the servlet response (filters bypass the advice). */
internal fun writeContractEnvelope(
    response: HttpServletResponse,
    status: HttpStatus,
    code: String,
    message: String,
) {
    if (response.isCommitted) return
    response.status = status.value()
    response.contentType = MediaType.APPLICATION_JSON_VALUE
    response.characterEncoding = Charsets.UTF_8.name()
    val error = linkedMapOf<String, Any?>(
        "code" to code,
        "message" to message,
        "request_id" to (MDC.get(TraceContextFilter.MDC_REQUEST_ID) ?: UUID.randomUUID().toString()),
        "trace_id" to (MDC.get(TraceContextFilter.MDC_TRACE_ID) ?: UUID.randomUUID().toString().replace("-", "")),
        "timestamp" to Instant.now().toString(),
    )
    SECURITY_ENVELOPE_MAPPER.writeValue(response.outputStream, mapOf("error" to error))
    response.outputStream.flush()
}

/**
 * 401 for missing/invalid bearer on a protected route (Contract 1/2). Without this, an
 * authenticated-only endpoint hit anonymously returns Spring's default 403 + empty body.
 */
@Component
class ContractAuthenticationEntryPoint : AuthenticationEntryPoint {
    override fun commence(
        request: HttpServletRequest,
        response: HttpServletResponse,
        authException: AuthenticationException,
    ) {
        log.debug("unauthenticated request to {} {}: {}", request.method, request.requestURI, authException.message)
        writeContractEnvelope(
            response,
            HttpStatus.UNAUTHORIZED,
            "UNAUTHORIZED",
            "Authentication is required to access this resource.",
        )
    }

    private companion object {
        val log = LoggerFactory.getLogger(ContractAuthenticationEntryPoint::class.java)
    }
}

/**
 * 403 for an authenticated principal lacking the required authority (Contract 2). Without this,
 * Spring returns a 403 with an empty body instead of the envelope.
 */
@Component
class ContractAccessDeniedHandler : AccessDeniedHandler {
    override fun handle(
        request: HttpServletRequest,
        response: HttpServletResponse,
        accessDeniedException: AccessDeniedException,
    ) {
        log.debug("access denied to {} {}: {}", request.method, request.requestURI, accessDeniedException.message)
        writeContractEnvelope(
            response,
            HttpStatus.FORBIDDEN,
            "FORBIDDEN",
            "You do not have permission to access this resource.",
        )
    }

    private companion object {
        val log = LoggerFactory.getLogger(ContractAccessDeniedHandler::class.java)
    }
}
