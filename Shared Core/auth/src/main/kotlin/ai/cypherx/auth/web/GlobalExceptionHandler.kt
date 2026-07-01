package ai.cypherx.auth.web

import org.slf4j.LoggerFactory
import org.slf4j.MDC
import org.springframework.http.HttpHeaders
import org.springframework.http.HttpStatus
import org.springframework.http.ResponseEntity
import org.springframework.http.converter.HttpMessageNotReadableException
import org.springframework.web.HttpMediaTypeNotSupportedException
import org.springframework.web.HttpRequestMethodNotSupportedException
import org.springframework.web.bind.MethodArgumentNotValidException
import org.springframework.web.bind.MissingServletRequestParameterException
import org.springframework.web.bind.annotation.ExceptionHandler
import org.springframework.web.bind.annotation.RestControllerAdvice
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException
import org.springframework.web.servlet.NoHandlerFoundException
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Renders every error as the Contract 2 envelope:
 *
 *     { "error": { "code", "message", "details?", "request_id", "trace_id", "timestamp" } }
 *
 * `request_id` and `trace_id` are read from the MDC ([TraceContextFilter] populates them). Bean
 * validation (`@Valid`) maps to VALIDATION_ERROR (422); body-parse / missing-param map to the
 * same; [ApiException] carries its own code + HTTP status; anything else is INTERNAL_ERROR (500).
 */
@RestControllerAdvice
class GlobalExceptionHandler {

    /** Feature code's primary path: a fully-specified Contract 2 error. */
    @ExceptionHandler(ApiException::class)
    fun handleApi(ex: ApiException): ResponseEntity<Map<String, Any?>> {
        if (ex.httpStatus.is5xxServerError) {
            log.error("ApiException {} {}: {}", ex.httpStatus.value(), ex.code, ex.message, ex)
        } else {
            log.debug("ApiException {} {}: {}", ex.httpStatus.value(), ex.code, ex.message)
        }
        return render(ex.httpStatus, ex.code, ex.message, ex.details)
    }

    /** @Valid request-body / @Validated argument failures → 422 VALIDATION_ERROR. */
    @ExceptionHandler(MethodArgumentNotValidException::class)
    fun handleValidation(ex: MethodArgumentNotValidException): ResponseEntity<Map<String, Any?>> {
        val fieldErrors = ex.bindingResult.fieldErrors.associate { it.field to (it.defaultMessage ?: "invalid") }
        return render(
            HttpStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_ERROR",
            "Request validation failed",
            mapOf("fields" to fieldErrors),
        )
    }

    /** Unparseable JSON body / wrong types → 422 VALIDATION_ERROR (do not leak parser internals). */
    @ExceptionHandler(HttpMessageNotReadableException::class)
    fun handleUnreadable(ex: HttpMessageNotReadableException): ResponseEntity<Map<String, Any?>> =
        render(HttpStatus.UNPROCESSABLE_ENTITY, "VALIDATION_ERROR", "Malformed or unreadable request body", null)

    /** Missing required query param → 422. */
    @ExceptionHandler(MissingServletRequestParameterException::class)
    fun handleMissingParam(ex: MissingServletRequestParameterException): ResponseEntity<Map<String, Any?>> =
        render(
            HttpStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_ERROR",
            "Missing required parameter: ${ex.parameterName}",
            mapOf("parameter" to ex.parameterName),
        )

    /** Query param type mismatch (e.g. non-UUID where UUID expected) → 422. */
    @ExceptionHandler(MethodArgumentTypeMismatchException::class)
    fun handleTypeMismatch(ex: MethodArgumentTypeMismatchException): ResponseEntity<Map<String, Any?>> =
        render(
            HttpStatus.UNPROCESSABLE_ENTITY,
            "VALIDATION_ERROR",
            "Invalid value for parameter: ${ex.name}",
            mapOf("parameter" to ex.name),
        )

    /**
     * Unknown route → 404 NOT_FOUND (Contract 2). Requires
     * `spring.mvc.throw-exception-if-no-handler-found=true` + no auto static-resource mapping so the
     * DispatcherServlet raises this instead of forwarding to a (missing) default servlet → 500.
     */
    @ExceptionHandler(NoHandlerFoundException::class)
    fun handleNoHandler(ex: NoHandlerFoundException): ResponseEntity<Map<String, Any?>> {
        log.debug("no handler for {} {}", ex.httpMethod, ex.requestURL)
        return render(
            HttpStatus.NOT_FOUND,
            "NOT_FOUND",
            "No handler for ${ex.httpMethod} ${ex.requestURL}",
            null,
        )
    }

    /**
     * Unsupported HTTP method on a known path → 405 METHOD_NOT_ALLOWED with the `Allow` header listing
     * the supported methods (Contract 2). The catch-all would otherwise turn this into a 500 with an
     * empty Allow header.
     */
    @ExceptionHandler(HttpRequestMethodNotSupportedException::class)
    fun handleMethodNotSupported(ex: HttpRequestMethodNotSupportedException): ResponseEntity<Map<String, Any?>> {
        log.debug("method {} not supported; supported={}", ex.method, ex.supportedMethods?.joinToString(","))
        val allow = ex.supportedMethods?.joinToString(", ") ?: ""
        return render(
            HttpStatus.METHOD_NOT_ALLOWED,
            "METHOD_NOT_ALLOWED",
            "HTTP method ${ex.method} is not supported for this resource",
            ex.supportedMethods?.let { mapOf("allowed_methods" to it.toList()) },
        ) { headers -> if (allow.isNotEmpty()) headers.set(HttpHeaders.ALLOW, allow) }
    }

    /** Unsupported request Content-Type on a known path → 415 UNSUPPORTED_MEDIA_TYPE (Contract 2). */
    @ExceptionHandler(HttpMediaTypeNotSupportedException::class)
    fun handleMediaTypeNotSupported(ex: HttpMediaTypeNotSupportedException): ResponseEntity<Map<String, Any?>> {
        log.debug("unsupported media type {}; supported={}", ex.contentType, ex.supportedMediaTypes)
        return render(
            HttpStatus.UNSUPPORTED_MEDIA_TYPE,
            "UNSUPPORTED_MEDIA_TYPE",
            "Content-Type ${ex.contentType ?: "<none>"} is not supported",
            ex.supportedMediaTypes
                .takeIf { it.isNotEmpty() }
                ?.let { mapOf("supported_media_types" to it.map(Any::toString)) },
        )
    }

    /** Last-resort catch-all → 500 INTERNAL_ERROR; the real cause is logged, never surfaced. */
    @ExceptionHandler(Exception::class)
    fun handleUnexpected(ex: Exception): ResponseEntity<Map<String, Any?>> {
        log.error("unhandled exception", ex)
        return render(HttpStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "An unexpected error occurred", null)
    }

    // ─────────────────────────────────────────────────────────────────────────────────────

    private fun render(
        status: HttpStatus,
        code: String,
        message: String,
        details: Map<String, Any?>?,
        headers: (HttpHeaders) -> Unit = {},
    ): ResponseEntity<Map<String, Any?>> {
        val error = linkedMapOf<String, Any?>(
            "code" to code,
            "message" to message,
        )
        if (details != null) error["details"] = details
        error["request_id"] = mdcOrRandomUuid(TraceContextFilter.MDC_REQUEST_ID)
        error["trace_id"] = MDC.get(TraceContextFilter.MDC_TRACE_ID) ?: newTraceId()
        error["timestamp"] = TIMESTAMP_FMT.format(Instant.now())
        return ResponseEntity.status(status)
            .headers(HttpHeaders().apply(headers))
            .body(mapOf("error" to error))
    }

    /** request_id is a UUID (Contract 2). Fall back to a fresh UUID when the MDC has none. */
    private fun mdcOrRandomUuid(key: String): String =
        MDC.get(key)?.takeIf { it.isNotBlank() } ?: UUID.randomUUID().toString()

    /** trace_id is a W3C trace id (32 hex). Fall back to a synthetic one when absent. */
    private fun newTraceId(): String = UUID.randomUUID().toString().replace("-", "")

    private companion object {
        val log = LoggerFactory.getLogger(GlobalExceptionHandler::class.java)
        val TIMESTAMP_FMT: DateTimeFormatter = DateTimeFormatter.ISO_INSTANT
    }
}
