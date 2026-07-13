"""MCP tool-invocation client (tool-loop stage, WP12).

Invokes an MCP tool (e.g. tool-web-search) over real MCP (JSON-RPC 2.0 / Streamable HTTP) at
the ``invoke_url`` resolved from the Tool Registry:

  * ``GET  {invoke_url}/manifest``   -> the tool's MCP manifest
  * ``POST {mcp_url}`` (``initialize`` -> ``tools/call``) -> the tool result

Identity flows via HEADERS only (Contract 13) — no identity in the body:

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent`` + ``tracestate`` + ``X-Request-ID`` (Contract 8 W3C propagation)
  * ``Idempotency-Key: {task_id}:{tool_call_id}``       (Contract 9 — a retried/duplicated
    invocation of the SAME tool call is de-duplicated by the tool, so a network retry
    can't double-charge a side-effecting tool)

── Circuit breaker (per ``(endpoint, agent)``) ───────────────────────────────────────────
Each ``(invoke_url, on_behalf_of-or-agent)`` pair has its own breaker:
  * CLOSED   — calls flow; ``mcp_circuit_breaker_threshold`` CONSECUTIVE failures open it.
  * OPEN     — calls fast-fail SERVICE_UNAVAILABLE (NO network) until the cooldown
               (``mcp_circuit_breaker_cooldown_seconds``) elapses; then HALF-OPEN.
  * HALF-OPEN— one trial call is allowed; success CLOSES + resets the breaker, failure
               RE-OPENS it for another cooldown.
A "failure" is a transport error or a 5xx (server fault). A 4xx is a CLIENT fault (bad
args / unauthorized) — it does NOT trip the breaker and is NEVER retried.

── Retry ─────────────────────────────────────────────────────────────────────────────────
Within a single ``invoke``, a connection error / 5xx is retried up to
``mcp_retry_attempts`` times (same Idempotency-Key each time, so retries are safe). A 4xx
is terminal on the first response. Retries are bounded by the breaker — once enough
consecutive failures accrue the breaker opens and subsequent calls fast-fail.

── FAIL POSTURE ───────────────────────────────────────────────────────────────────────────
A tool is an OPTIONAL capability, so all failure modes raise a typed ``ApiError`` for the
tool-loop stage to handle (record the tool result as an error + let the LLM proceed)
rather than being swallowed: a terminal 4xx -> VALIDATION_ERROR (carries the upstream
status in ``details``), an open breaker / exhausted-retry transport-or-5xx ->
SERVICE_UNAVAILABLE. The stage decides whether a failed tool call is fatal to the task.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class McpResult:
    """Normalised MCP tool-invocation result."""

    tool: str
    result: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


class _Breaker:
    """A single (endpoint, agent) circuit breaker (CLOSED / OPEN / HALF-OPEN)."""

    __slots__ = ("consecutive_failures", "opened_at")

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.opened_at: float | None = None  # monotonic time the breaker tripped open


class McpClient:
    """Thin async client for invoking MCP tools, with a per-(endpoint, agent) breaker."""

    def __init__(
        self,
        settings: Settings,
        token_provider: ServiceTokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client  # injectable for tests (respx); lazily created otherwise
        self._owns_client = client is None
        self._breakers: dict[tuple[str, str], _Breaker] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.mcp_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _headers(
        self, *, agent_jwt: str, on_behalf_of: str | None, idempotency_key: str | None = None
    ) -> dict[str, str]:
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "X-Forwarded-Agent-JWT": agent_jwt,
            **trace.propagation_headers(),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    # ── Circuit-breaker helpers ───────────────────────────────────────────────────
    def _breaker_for(self, invoke_url: str, agent_key: str) -> _Breaker:
        key = (invoke_url, agent_key)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = _Breaker()
            self._breakers[key] = breaker
        return breaker

    def _is_open(self, breaker: _Breaker, invoke_url: str) -> bool:
        """True while the breaker is OPEN (cooldown not yet elapsed).

        Once the cooldown passes we report False (HALF-OPEN) and allow one trial call;
        the trial's outcome is recorded via ``_record_success`` / ``_record_failure``.
        """
        if breaker.opened_at is None:
            return False
        # Within cooldown -> still OPEN; once it elapses report False (HALF-OPEN: a trial runs).
        elapsed = time.monotonic() - breaker.opened_at
        return elapsed < self._settings.mcp_circuit_breaker_cooldown_seconds

    def _record_success(self, breaker: _Breaker, invoke_url: str) -> None:
        breaker.consecutive_failures = 0
        if breaker.opened_at is not None:
            breaker.opened_at = None  # half-open trial succeeded -> CLOSE
            metrics.mcp_circuit_breaker_state.labels(invoke_url).set(0)

    def _record_failure(self, breaker: _Breaker, invoke_url: str) -> None:
        breaker.consecutive_failures += 1
        threshold = self._settings.mcp_circuit_breaker_threshold
        # A half-open trial failure (already open) re-opens; or we cross the threshold.
        if breaker.opened_at is not None or breaker.consecutive_failures >= threshold:
            breaker.opened_at = time.monotonic()
            metrics.mcp_circuit_breaker_state.labels(invoke_url).set(1)
            logger.warning(
                "mcp_circuit_open",
                endpoint=invoke_url,
                consecutive_failures=breaker.consecutive_failures,
            )

    # ── Public API ─────────────────────────────────────────────────────────────────
    async def get_manifest(
        self,
        invoke_url: str,
        *,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the tool's MCP manifest (``GET {invoke_url}/manifest``).

        Not breaker-gated (a read with no side effects). Raises SERVICE_UNAVAILABLE on a
        transport/5xx error, VALIDATION_ERROR on a 4xx.
        """
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        url = f"{invoke_url.rstrip('/')}/manifest"
        try:
            resp = await self._http().get(url, headers=headers)
        except httpx.HTTPError as exc:
            metrics.mcp_invocations_total.labels("error").inc()
            logger.warning("mcp_manifest_failed", endpoint=invoke_url, error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "MCP tool unavailable.") from exc
        if 400 <= resp.status_code < 500:
            metrics.mcp_invocations_total.labels("rejected").inc()
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"MCP tool rejected manifest request ({resp.status_code}).",
                details={"status": resp.status_code},
            )
        if resp.status_code >= 500:
            metrics.mcp_invocations_total.labels("error").inc()
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"MCP tool returned {resp.status_code}.")
        return resp.json()

    async def invoke_mcp(
        self,
        mcp_url: str,
        tool: str,
        args: dict[str, Any],
        *,
        task_id: str,
        tool_call_id: str,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> McpResult:
        """Invoke ``tool`` over real MCP (JSON-RPC 2.0 / Streamable HTTP) at ``mcp_url``.

        Performs the MCP handshake (``initialize`` -> ``tools/call``) per call and maps the
        result back to :class:`McpResult`, carrying the SAME identity headers, Idempotency-Key
        (``{task_id}:{tool_call_id}``), per-(endpoint, agent) circuit breaker, and conn/5xx-only
        retry as :meth:`get_manifest` — MCP is the sole tool wire (the legacy direct-HTTP invoke
        endpoint was removed). A tool-level ``isError`` result is raised as an ApiError
        (SERVICE_UNAVAILABLE when ``_meta.retryable`` else VALIDATION_ERROR) so the tool-loop
        stage handles it exactly like a legacy failure.
        """
        agent_key = on_behalf_of or agent_jwt
        breaker = self._breaker_for(mcp_url, agent_key)
        if self._is_open(breaker, mcp_url):
            metrics.mcp_invocations_total.labels("circuit_open").inc()
            logger.warning("mcp_circuit_open_fastfail", endpoint=mcp_url, tool=tool)
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"MCP tool circuit open for {mcp_url!r}.")

        idempotency_key = f"{task_id}:{tool_call_id}"
        headers = await self._headers(
            agent_jwt=agent_jwt, on_behalf_of=on_behalf_of, idempotency_key=idempotency_key
        )
        headers["Accept"] = "application/json, text/event-stream"
        init_body = _initialize_request(self._settings.service_version)
        call_body = _tools_call_request(tool, args, tool_call_id)

        attempts = max(1, self._settings.mcp_retry_attempts + 1)
        last_exc: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                init_resp = await self._http().post(mcp_url, headers=headers, json=init_body)
                _raise_for_mcp_transport(init_resp, tool)  # 4xx terminal; 5xx -> _RetryableHttp
                session = init_resp.headers.get("mcp-session-id")
                call_headers = {**headers, "Mcp-Session-Id": session} if session else headers
                resp = await self._http().post(mcp_url, headers=call_headers, json=call_body)
                _raise_for_mcp_transport(resp, tool)
            except httpx.HTTPError as exc:
                last_exc = exc
                self._record_failure(breaker, mcp_url)
                logger.warning(
                    "mcp_invoke_attempt_failed", endpoint=mcp_url, attempt=attempt, error=str(exc)
                )
                if self._is_open(breaker, mcp_url):
                    break
                continue
            except _RetryableHttp as exc:
                self._record_failure(breaker, mcp_url)
                logger.warning("mcp_invoke_5xx", endpoint=mcp_url, attempt=attempt, status=exc.status)
                if attempt < attempts - 1 and not self._is_open(breaker, mcp_url):
                    continue
                metrics.mcp_invocations_total.labels("error").inc()
                raise ApiError(
                    ErrorCode.SERVICE_UNAVAILABLE, f"MCP tool returned {exc.status}."
                ) from exc

            # 2xx on both initialize + tools/call — parse the JSON-RPC result.
            self._record_success(breaker, mcp_url)
            metrics.mcp_invocations_total.labels("ok").inc()
            return _parse_tools_call(tool, resp)

        metrics.mcp_invocations_total.labels("error").inc()
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "MCP tool unavailable.") from last_exc


# ── MCP (JSON-RPC 2.0 / Streamable HTTP) wire helpers ───────────────────────────────────────
_MCP_PROTOCOL_VERSION = "2025-06-18"


class _RetryableHttp(Exception):
    """Internal signal: an MCP POST returned a retryable 5xx (server fault)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _initialize_request(client_version: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "mcp-init",
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "xagent", "version": client_version},
        },
    }


def _tools_call_request(tool: str, args: dict[str, Any], req_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }


def _raise_for_mcp_transport(resp: httpx.Response, tool: str) -> None:
    """Classify an MCP POST's HTTP status: 4xx terminal (ApiError), 5xx -> retryable signal."""
    if 400 <= resp.status_code < 500:
        # CLIENT fault (auth/forbidden/bad request) — terminal, NEVER retried, no breaker trip.
        metrics.mcp_invocations_total.labels("rejected").inc()
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"MCP tool rejected the call ({resp.status_code}).",
            details={"status": resp.status_code, "tool": tool},
        )
    if resp.status_code >= 500:
        raise _RetryableHttp(resp.status_code)


def _read_jsonrpc(resp: httpx.Response) -> dict[str, Any]:
    """Read a single JSON-RPC message from a JSON or Streamable-HTTP (SSE) response body."""
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/event-stream"):
        message: str | None = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                message = line[len("data:") :].strip()  # last data: line is the response
        try:
            return json.loads(message) if message else {}
        except (ValueError, TypeError):
            return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _first_text(content: Any) -> str | None:
    """Return the first ``text`` block of an MCP content array, if any."""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    return None


def _parse_tools_call(tool: str, resp: httpx.Response) -> McpResult:
    """Map an MCP ``tools/call`` JSON-RPC response to :class:`McpResult` (or raise ApiError).

    Protocol errors and tool ``isError`` results raise a typed ApiError so the tool-loop stage
    handles them identically to a legacy failure; ``_meta.retryable`` preserves retryability.
    """
    data = _read_jsonrpc(resp)
    if data.get("error"):
        err = data["error"]
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"MCP protocol error: {err.get('message', 'unknown')}",
            details={"jsonrpc_code": err.get("code"), "tool": tool},
        )
    result = data.get("result") or {}
    if result.get("isError"):
        meta = result.get("_meta") or {}
        message = _first_text(result.get("content")) or "MCP tool reported an error."
        if meta.get("retryable"):
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, message, details={"code": meta.get("code")})
        raise ApiError(ErrorCode.VALIDATION_ERROR, message, details={"code": meta.get("code")})
    structured = result.get("structuredContent")
    if structured is None:
        text = _first_text(result.get("content"))
        if text is not None:
            try:
                structured = json.loads(text)
            except (ValueError, TypeError):
                structured = {"output": text}
    return McpResult(tool=tool, result=structured, raw=data)
