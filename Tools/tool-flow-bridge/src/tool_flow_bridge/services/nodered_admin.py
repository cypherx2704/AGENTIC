"""Node-RED Admin API client + flow-shape validation.

Used by the publish pipeline to read the workflow the user built and confirm it is
tool-shaped: exactly one enabled ``http in`` node (the synchronous trigger) reachable to at
least one ``http response`` node. Returns the trigger's method + path so the bridge knows
which HTTP-In endpoint to route invocations to.

Admin API lives under ``httpAdminRoot`` (``nodered_admin_root``, default ``/nodered``),
which is deliberately disjoint from the HTTP-In root (``http_node_root``, default ``/flow``)
so ``GET /nodered/flow/:id`` (admin) never collides with ``/flow/*`` (node endpoints).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from ..core import metrics
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FlowShape:
    """The tool-relevant facts extracted from a Node-RED flow tab."""

    http_method: str
    http_path: str


class NoderedAdmin:
    """Thin async client over the Node-RED Admin API for one runtime."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def _admin_url(self, internal_host: str, path: str) -> str:
        root = self._settings.nodered_admin_root.rstrip("/")
        return f"{internal_host.rstrip('/')}{root}{path}"

    def _headers(self, admin_token: str) -> dict[str, str]:
        return {"Authorization": f"{self._settings.nodered_admin_scheme} {admin_token}"}

    async def list_flow_tabs(
        self, *, internal_host: str, admin_token: str
    ) -> list[dict[str, str]]:
        """GET /flows -> the enabled flow tabs as ``[{id, label}]`` (for the publish picker)."""
        url = self._admin_url(internal_host, "/flows")
        try:
            resp = await self._client.get(
                url,
                headers=self._headers(admin_token),
                timeout=self._settings.nodered_admin_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            metrics.nodered_admin_total.labels("list_flows", "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, f"Node-RED runtime unreachable: {exc}"
            ) from exc
        if resp.status_code >= 400:
            metrics.nodered_admin_total.labels("list_flows", "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, f"Node-RED Admin API error ({resp.status_code})."
            )
        metrics.nodered_admin_total.labels("list_flows", "ok").inc()
        nodes = resp.json()
        tabs = nodes if isinstance(nodes, list) else nodes.get("flows", [])
        return [
            {"id": n["id"], "label": n.get("label") or n["id"]}
            for n in tabs
            if isinstance(n, dict) and n.get("type") == "tab" and not n.get("disabled", False)
        ]

    async def get_flow(
        self, *, internal_host: str, admin_token: str, flow_id: str
    ) -> dict[str, Any]:
        """GET /flow/:id — the flow tab object (``{id, label, nodes: [...]}``)."""
        url = self._admin_url(internal_host, f"/flow/{flow_id}")
        try:
            resp = await self._client.get(
                url,
                headers=self._headers(admin_token),
                timeout=self._settings.nodered_admin_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            metrics.nodered_admin_total.labels("get_flow", "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, f"Node-RED runtime unreachable: {exc}"
            ) from exc
        if resp.status_code == 404:
            metrics.nodered_admin_total.labels("get_flow", "not_found").inc()
            raise ApiError(ErrorCode.NOT_FOUND, f"Workflow '{flow_id}' not found in Node-RED.")
        if resp.status_code >= 400:
            metrics.nodered_admin_total.labels("get_flow", "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Node-RED Admin API error ({resp.status_code}).",
            )
        metrics.nodered_admin_total.labels("get_flow", "ok").inc()
        return resp.json()

    async def redeploy_flow(
        self, *, internal_host: str, admin_token: str, flow_id: str, flow: dict[str, Any]
    ) -> bool:
        """PUT /flow/:id — (re)deploy this flow tab so its ``http in`` route is registered and
        live before we publish. ``get_flow`` already returns the deployed runtime state, so this
        is defensive belt-and-suspenders (e.g. a route dropped after a runtime restart); it is
        therefore BEST-EFFORT — a failure is logged and does not fail the publish.

        Returns True if the flow was (re)deployed, False if the attempt failed.
        """
        url = self._admin_url(internal_host, f"/flow/{flow_id}")
        headers = {**self._headers(admin_token), "Content-Type": "application/json"}
        try:
            resp = await self._client.put(
                url, json=flow, headers=headers,
                timeout=self._settings.nodered_admin_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            metrics.nodered_admin_total.labels("redeploy_flow", "error").inc()
            logger.warning("nodered_redeploy_unreachable", flow_id=flow_id, error=str(exc))
            return False
        if resp.status_code >= 400:
            metrics.nodered_admin_total.labels("redeploy_flow", "error").inc()
            logger.warning("nodered_redeploy_failed", flow_id=flow_id, status=resp.status_code)
            return False
        metrics.nodered_admin_total.labels("redeploy_flow", "ok").inc()
        return True


def _reaches_any(start: dict[str, Any], targets: set[str], nodes: list[Any]) -> bool:
    """BFS the Node-RED wire graph from ``start`` and return True if it reaches any id in ``targets``.

    A node's ``wires`` is a list (one entry per output) of lists of downstream node ids. We also
    follow ``link out`` -> ``link in`` hops (Node-RED's virtual wires) via each link node's ``links``.
    """
    by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}
    seen: set[str] = set()
    frontier = [start["id"]] if "id" in start else []
    while frontier:
        nid = frontier.pop()
        if nid in seen:
            continue
        seen.add(nid)
        if nid in targets:
            return True
        node = by_id.get(nid)
        if not node:
            continue
        for output in node.get("wires") or []:
            if isinstance(output, list):
                frontier.extend(t for t in output if isinstance(t, str))
        # `link out` nodes jump to the `link in` nodes listed in `links` (virtual wires).
        if node.get("type") == "link out":
            frontier.extend(t for t in (node.get("links") or []) if isinstance(t, str))
    return False


def validate_flow_shape(flow: dict[str, Any]) -> FlowShape:
    """Confirm the flow is tool-shaped and return the HTTP-In trigger's method + path.

    Requires exactly one ENABLED ``http in`` node and at least one ``http response`` node.
    Raises 422 VALIDATION_ERROR with an actionable message otherwise.
    """
    nodes = flow.get("nodes") or []
    if not isinstance(nodes, list):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Malformed workflow.", status_code=422)

    def _enabled(n: dict[str, Any]) -> bool:
        # Node-RED marks a disabled node with d == True.
        return not n.get("d", False)

    http_ins = [
        n for n in nodes if isinstance(n, dict) and n.get("type") == "http in" and _enabled(n)
    ]
    http_responses = [
        n for n in nodes
        if isinstance(n, dict) and n.get("type") == "http response" and _enabled(n)
    ]

    if len(http_ins) == 0:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "The workflow needs an 'http in' node as its trigger. Add one (method POST, "
            "a URL path) so the tool can be called.",
            status_code=422,
            details={"reason": "MISSING_HTTP_IN"},
        )
    if len(http_ins) > 1:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "The workflow has more than one 'http in' node; a tool must have exactly one "
            "trigger. Keep a single 'http in' node per published tool.",
            status_code=422,
            details={"reason": "MULTIPLE_HTTP_IN"},
        )
    if len(http_responses) == 0:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "The workflow needs an 'http response' node so the tool returns a result. Wire "
            "the flow's output into an 'http response' node.",
            status_code=422,
            details={"reason": "MISSING_HTTP_RESPONSE"},
        )

    # An http-response must be actually REACHABLE from the http-in (following `wires`), not merely
    # present — otherwise the tool publishes but every invocation hangs until timeout (503) because
    # nothing ever responds. BFS the wire graph from the trigger and require it to reach a response.
    response_ids = {n["id"] for n in http_responses if "id" in n}
    if not _reaches_any(http_ins[0], response_ids, nodes):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "The 'http in' trigger isn't connected to an 'http response' node. Wire the flow "
            "from the trigger through your logic to an 'http response' so the tool can reply.",
            status_code=422,
            details={"reason": "HTTP_RESPONSE_UNREACHABLE"},
        )

    node = http_ins[0]
    path = node.get("url")
    method = str(node.get("method", "post")).upper()
    if not isinstance(path, str) or not path.startswith("/"):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "The 'http in' node must have a URL path starting with '/'.",
            status_code=422,
            details={"reason": "INVALID_HTTP_IN_PATH"},
        )
    return FlowShape(http_method=method, http_path=path)
