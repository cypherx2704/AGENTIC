"""Node-RED execution adapter — the ONLY engine-specific code on the invoke path.

Routes an MCP tool call to the tenant's Node-RED HTTP-In endpoint and returns its JSON
response. This is the seam that makes the engine swappable: replace this module (and the
admin client) to target Elsa / Langflow / any HTTP-triggerable engine; nothing else on the
MCP/registry/invoke path changes.

Response mapping (consumed by the invoke handler and, ultimately, xAgent's retry logic):
* 2xx JSON            -> returned as the tool ``result``.
* 2xx non-JSON        -> wrapped as ``{"output": "<text>"}``.
* 4xx                 -> NoderedError(retryable=False) -> bridge 422 (TERMINAL; the flow
                        rejected the input, xAgent must not retry).
* 5xx / timeout / conn -> NoderedError(retryable=True) -> bridge 502/503 (xAgent retries
                        with the same Idempotency-Key; the flow's own idempotency + our
                        replay cache prevent double-firing).
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..core import metrics

logger = structlog.get_logger(__name__)


class NoderedError(Exception):
    """A Node-RED execution failure with a retry hint for the invoke handler."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


#: Bound on how much of a flow's error body we echo (it is author-controlled text, not a payload).
_MAX_DETAIL_CHARS = 300
#: Keys a flow conventionally puts its human-readable failure in.
_DETAIL_KEYS = ("error", "message", "detail", "reason")


def _flow_error_detail(resp: httpx.Response) -> str:
    """Extract the WORKFLOW'S OWN error message from its response body.

    A flow that answers ``{"error": "topic not found"}`` is saying exactly what went wrong.
    Swallowing that and reporting only "HTTP 404" sends the author hunting for a broken tool or a
    missing API key when the upstream simply had no such record — so echo what the flow said.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:_MAX_DETAIL_CHARS]

    if isinstance(body, dict):
        for key in _DETAIL_KEYS:
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_DETAIL_CHARS]
    if isinstance(body, str) and body.strip():
        return body.strip()[:_MAX_DETAIL_CHARS]
    return ""


def _client_error_message(status_code: int, detail: str) -> str:
    """Phrase a 4xx so the author can act on it.

    "Workflow rejected the request" is wrong for the common case: a 404 usually means the flow ran
    fine and the UPSTREAM API had no such record (a typo'd topic, a missing repo). Name that.
    """
    if not detail:
        return f"Workflow returned HTTP {status_code}."
    if status_code == 404:
        return f"Workflow ran, but the requested item was not found (HTTP 404): {detail}"
    return f"Workflow returned an error (HTTP {status_code}): {detail}"


async def invoke_workflow(
    client: httpx.AsyncClient,
    *,
    internal_host: str,
    http_node_root: str,
    http_path: str,
    method: str,
    args: dict[str, Any],
    secret: str,
    secret_header: str,
    timeout: float,
    trace_headers: dict[str, str] | None = None,
) -> Any:
    """POST the tool args to the workflow's HTTP-In endpoint; return its parsed result."""
    url = f"{internal_host.rstrip('/')}{http_node_root.rstrip('/')}{http_path}"
    headers = {secret_header: secret, "content-type": "application/json"}
    if trace_headers:
        headers.update(trace_headers)

    try:
        resp = await client.request(
            method.upper() or "POST", url, json=args, headers=headers, timeout=timeout
        )
    except httpx.TimeoutException as exc:
        metrics.nodered_invoke_total.labels("timeout").inc()
        raise NoderedError(f"Workflow execution timed out after {timeout}s.", retryable=True) from exc
    except httpx.RequestError as exc:
        metrics.nodered_invoke_total.labels("server_error").inc()
        raise NoderedError(f"Workflow runtime unreachable: {exc}", retryable=True) from exc

    if 200 <= resp.status_code < 300:
        metrics.nodered_invoke_total.labels("ok").inc()
        try:
            return resp.json()
        except ValueError:
            return {"output": resp.text}
    # fall through to the error paths below

    if 300 <= resp.status_code < 400:
        # A redirect is never a valid tool result and retrying won't change it -> terminal.
        metrics.nodered_invoke_total.labels("redirect").inc()
        logger.info("nodered_redirect", status=resp.status_code, path=http_path)
        raise NoderedError(
            f"Workflow returned a redirect (HTTP {resp.status_code}); the 'http response' node "
            "must return a final result, not a redirect.",
            retryable=False,
            status_code=resp.status_code,
        )

    if 400 <= resp.status_code < 500:
        metrics.nodered_invoke_total.labels("client_error").inc()
        detail = _flow_error_detail(resp)
        logger.info("nodered_client_error", status=resp.status_code, path=http_path, detail=detail)
        raise NoderedError(
            _client_error_message(resp.status_code, detail),
            retryable=False,
            status_code=resp.status_code,
        )

    metrics.nodered_invoke_total.labels("server_error").inc()
    detail = _flow_error_detail(resp)
    logger.warning("nodered_server_error", status=resp.status_code, path=http_path, detail=detail)
    raise NoderedError(
        f"Workflow runtime error (HTTP {resp.status_code})." + (f" {detail}" if detail else ""),
        retryable=True,
        status_code=resp.status_code,
    )
