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

    if 400 <= resp.status_code < 500:
        metrics.nodered_invoke_total.labels("client_error").inc()
        logger.info("nodered_client_error", status=resp.status_code, path=http_path)
        raise NoderedError(
            f"Workflow rejected the request (HTTP {resp.status_code}).",
            retryable=False,
            status_code=resp.status_code,
        )

    metrics.nodered_invoke_total.labels("server_error").inc()
    logger.warning("nodered_server_error", status=resp.status_code, path=http_path)
    raise NoderedError(
        f"Workflow runtime error (HTTP {resp.status_code}).",
        retryable=True,
        status_code=resp.status_code,
    )
