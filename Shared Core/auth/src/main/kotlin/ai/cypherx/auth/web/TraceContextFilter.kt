package ai.cypherx.auth.web

import jakarta.servlet.FilterChain
import jakarta.servlet.http.HttpServletRequest
import jakarta.servlet.http.HttpServletResponse
import org.slf4j.MDC
import org.springframework.core.Ordered
import org.springframework.core.annotation.Order
import org.springframework.stereotype.Component
import org.springframework.web.filter.OncePerRequestFilter
import java.util.UUID

/**
 * Populates the MDC for the duration of each request so structured logs (Contract 6) and the
 * Contract 2 error envelope can stamp the trace/request/tenant/agent ids (Contract 8).
 *
 * Extraction:
 *  - `trace_id`, `span_id` ← `traceparent` (`00-{32hex trace}-{16hex span}-{flags}`). If absent
 *    or malformed, a fresh 32-hex trace id is synthesised so every log line still correlates.
 *  - `request_id` ← `X-Request-ID` (UUID; Kong injects it). Synthesised if absent.
 *  - `tenant_id`  ← `X-Tenant-ID` (Kong injects from the JWT).
 *  - `agent_id`   ← `X-Agent-ID`  (Kong injects from the JWT).
 *
 * Runs very early (HIGHEST_PRECEDENCE + 10) so downstream filters/handlers see the MDC. The
 * `finally` block ALWAYS clears the MDC to prevent leakage across pooled request threads.
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 10)
class TraceContextFilter : OncePerRequestFilter() {

    override fun doFilterInternal(
        request: HttpServletRequest,
        response: HttpServletResponse,
        filterChain: FilterChain,
    ) {
        try {
            val (traceId, spanId) = parseTraceparent(request.getHeader(HDR_TRACEPARENT))
            MDC.put(MDC_TRACE_ID, traceId)
            spanId?.let { MDC.put(MDC_SPAN_ID, it) }

            val requestId = request.getHeader(HDR_REQUEST_ID)?.takeIf { it.isNotBlank() }
                ?: UUID.randomUUID().toString()
            MDC.put(MDC_REQUEST_ID, requestId)

            request.getHeader(HDR_TENANT_ID)?.takeIf { it.isNotBlank() }?.let { MDC.put(MDC_TENANT_ID, it) }
            request.getHeader(HDR_AGENT_ID)?.takeIf { it.isNotBlank() }?.let { MDC.put(MDC_AGENT_ID, it) }

            // Echo the request id back so callers can correlate even when Kong didn't set one.
            response.setHeader(HDR_REQUEST_ID, requestId)

            filterChain.doFilter(request, response)
        } finally {
            MDC.remove(MDC_TRACE_ID)
            MDC.remove(MDC_SPAN_ID)
            MDC.remove(MDC_REQUEST_ID)
            MDC.remove(MDC_TENANT_ID)
            MDC.remove(MDC_AGENT_ID)
        }
    }

    /** Returns (trace_id, span_id?) from a W3C `traceparent`, synthesising a trace id if absent. */
    private fun parseTraceparent(header: String?): Pair<String, String?> {
        if (header != null) {
            val parts = header.split("-")
            // 00-{32hex}-{16hex}-{2hex}
            if (parts.size >= 3 && parts[1].length == 32 && parts[2].length == 16) {
                return parts[1] to parts[2]
            }
        }
        return newTraceId() to null
    }

    private fun newTraceId(): String = UUID.randomUUID().toString().replace("-", "")

    companion object {
        const val HDR_TRACEPARENT = "traceparent"
        const val HDR_REQUEST_ID = "X-Request-ID"
        const val HDR_TENANT_ID = "X-Tenant-ID"
        const val HDR_AGENT_ID = "X-Agent-ID"

        const val MDC_TRACE_ID = "trace_id"
        const val MDC_SPAN_ID = "span_id"
        const val MDC_REQUEST_ID = "request_id"
        const val MDC_TENANT_ID = "tenant_id"
        const val MDC_AGENT_ID = "agent_id"
    }
}
