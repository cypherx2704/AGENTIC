package ai.cypherx.auth.web

import org.springframework.http.HttpStatus

/**
 * The single exception type feature code throws to produce a Contract 2 error envelope.
 * The Core [GlobalExceptionHandler] renders it as
 *
 *     { "error": { "code", "message", "details?", "request_id", "trace_id", "timestamp" } }
 *
 * with [httpStatus] as the HTTP status.
 *
 * @param code      machine-readable SCREAMING_SNAKE_CASE code from the Contract 2 known-codes list.
 * @param httpStatus HTTP status to return.
 * @param message   human-readable, safe to surface (NO secrets / stack traces).
 * @param details   optional structured, code-specific detail map.
 *
 * Convenience factories cover the common cases; prefer them over the raw constructor so codes
 * and statuses stay consistent across the service.
 */
class ApiException(
    val code: String,
    val httpStatus: HttpStatus,
    override val message: String,
    val details: Map<String, Any?>? = null,
    cause: Throwable? = null,
) : RuntimeException(message, cause) {

    companion object {
        /** 401 — missing/invalid credentials. */
        fun unauthorized(message: String = "Unauthorized", details: Map<String, Any?>? = null) =
            ApiException("UNAUTHORIZED", HttpStatus.UNAUTHORIZED, message, details)

        /** 403 — authenticated but not permitted. */
        fun forbidden(message: String = "Forbidden", details: Map<String, Any?>? = null) =
            ApiException("FORBIDDEN", HttpStatus.FORBIDDEN, message, details)

        /** 404 — resource not found. */
        fun notFound(message: String = "Not found", details: Map<String, Any?>? = null) =
            ApiException("NOT_FOUND", HttpStatus.NOT_FOUND, message, details)

        /** 409 — conflict (e.g. duplicate unique key). */
        fun conflict(message: String = "Conflict", details: Map<String, Any?>? = null) =
            ApiException("CONFLICT", HttpStatus.CONFLICT, message, details)

        /** 422 — request failed semantic validation. */
        fun validation(message: String = "Validation failed", details: Map<String, Any?>? = null) =
            ApiException("VALIDATION_ERROR", HttpStatus.UNPROCESSABLE_ENTITY, message, details)

        /** 410 — gone (e.g. bootstrap token after sentinel). */
        fun gone(message: String = "Gone", details: Map<String, Any?>? = null) =
            ApiException("CONFLICT", HttpStatus.GONE, message, details)

        /** 503 — a required dependency is unavailable. */
        fun serviceUnavailable(message: String = "Service unavailable", details: Map<String, Any?>? = null) =
            ApiException("SERVICE_UNAVAILABLE", HttpStatus.SERVICE_UNAVAILABLE, message, details)

        /** 500 — unexpected internal error. */
        fun internal(message: String = "Internal error", details: Map<String, Any?>? = null) =
            ApiException("INTERNAL_ERROR", HttpStatus.INTERNAL_SERVER_ERROR, message, details)
    }
}
