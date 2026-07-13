"""Phase 6 — in-process cross-service END-TO-END test for the Private/Protected tool workflow.

Threads ONE shared artifact — the Contract-4 MCP manifest tool-flow-bridge emits — through all
three services, each driven by its REAL code path, with only the DBs and Node-RED faked:

  hop 1  flow-bridge  POST /v1/tools  (+ POST /v1/mcps)  --publish-->  intercept the manifest the
         publisher hands to registry_client.register()  (the SHARED ARTIFACT is captured here)
  hop 2  registry     POST /v1/tools  (register the captured manifest)  ->  GET /v1/tools
         (marketplace listing + visibility) ->  GET /v1/tools/{name} (resolve -> manifest +
         invoke_url + capabilities)
  hop 3  xAgent       run the REAL ToolLoopStage: the REAL RegistryClient resolves the tool from
         the registry ASGI app (hop 2), expands manifest.tools[] into offered tools, derives the
         mcp endpoint {invoke_url}{mcp.endpoint} = /m/<slug>/mcp, resolves per-capability access
         via the registry, and the REAL McpClient issues initialize + tools/call POINTED at the
         flow-bridge ASGI app's POST /m/<slug>/mcp  ->  governed pipeline  ->  Node-RED (mocked)

Everything network-facing that ISN'T one of the three services is mocked at exactly one seam:
  * the DBs (flow-bridge FakeStore + registry scripted pool double),
  * Node-RED (an httpx.MockTransport standing in for the tenant runtime's HTTP-In endpoint),
  * the LLM (a scripted completion list — the model is out of scope; it only picks tool calls).

Both service HTTP hops run over ``httpx.ASGITransport`` (in-process, no sockets); the xAgent
registry + MCP clients are the REAL clients pointed at those transports, so the resolution and
the invocation are genuine cross-service calls, not hand-mocked return values.

Two scenarios, covering the whole contract:
  * Scenario A — a single PRIVATE atomic tool (auto-singleton MCP), invoked successfully.
  * Scenario B — a PROTECTED MULTI-tool MCP (2 members): tools/call routes by member name, and a
    per-capability deny (member ``beta`` -> ``none`` denied, sibling ``alpha`` -> ``automated``
    invoked) is enforced fail-closed at the xAgent hop.

Run:  xAgent/ax-1/.venv/Scripts/python.exe -m pytest tests/e2e -v
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

# ── flow-bridge (service under test, hop 1 + hop 3 server) ──────────────────────────────
from tool_flow_bridge.api import mcp as fb_mcp_api
from tool_flow_bridge.core.auth import Principal as FbPrincipal
from tool_flow_bridge.core.auth import require_principal as fb_require_principal
from tool_flow_bridge.db import pool as fb_db_pool
from tool_flow_bridge.db import queries as fb_queries
from tool_flow_bridge.main import create_app as fb_create_app
from tool_flow_bridge.services import publisher as fb_pub
from tool_flow_bridge.services.nodered_admin import FlowShape

# ── registry (hop 2) ────────────────────────────────────────────────────────────────────
from tool_registry.core.auth import Principal as RgPrincipal
from tool_registry.core.auth import require_principal as rg_require_principal
from tool_registry.db import pool as rg_db_pool
from tool_registry.main import create_app as rg_create_app

# ── xAgent (hop 3 driver) — REAL stage + REAL clients ───────────────────────────────────
from agent_runtime.core.auth import Principal as AxPrincipal
from agent_runtime.core.config import get_settings as ax_get_settings
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps as ax_deps
from agent_runtime.core.stages.tool_loop import ToolLoopStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.models.task import STEP_TYPE_TOOL_CALL
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpClient
from agent_runtime.services.registry_client import RegistryClient

# Shared identity across services (UUID-shaped; tenant8 == "00000000" drives the slugs).
TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"

FB_ADMIN_SCOPES = ["tool:invoke", "tool:admin", "tenant:admin", "platform:admin"]


# ══════════════════════════════════════════════════════════════════════════════════════
# flow-bridge fakes — FakeStore + FakeRegistry (mirrors tests/test_mcp_management.py)
# ══════════════════════════════════════════════════════════════════════════════════════
class FbFakeValkey:
    """Network-free Valkey double (idempotency + rate-limit both fail-open through it)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str, *, timeout_seconds: float | None = None) -> str | None:
        return self.store.get(key)

    async def set(self, key, value, *, ttl_seconds=None, timeout_seconds=None) -> None:  # type: ignore[no-untyped-def]
        self.store[key] = value

    async def incr_with_expire(self, key, *, ttl_seconds, timeout_seconds=None) -> int:  # type: ignore[no-untyped-def]
        n = int(self.store.get(key, "0")) + 1
        self.store[key] = str(n)
        return n


class FbFakeStore:
    """In-memory stand-in for the flow_tools tables (the new tools+mcps+mcp_tools model)."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.mcps: dict[str, dict] = {}
        self.links: set[tuple[str, str]] = set()
        self._seq = 0

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def add_tool(self, **over) -> dict:
        tid = over.get("tool_id") or self._id("tid")
        row = {
            "tool_id": tid, "tenant_id": TENANT, "snake_name": "t", "display_name": "T",
            "description": "d", "input_schema": {"type": "object", "properties": {}},
            "output_schema": None, "node_red_flow_id": "f1", "http_method": "POST",
            "http_path": "/x", "runtime_id": "rt1", "visibility": "private",
            "access_mode": "automated", "version": "1.0.0", "status": "active",
            "updated_at": _now(), "internal_host": "http://nodered:1880",
            "http_node_root": "/flow", "invoke_secret_ref": "static:invoke",
        }
        row.update(over)
        self.tools[tid] = row
        return row

    def add_mcp(self, **over) -> dict:
        mid = over.get("mcp_id") or self._id("mid")
        row = {
            "mcp_id": mid, "tenant_id": TENANT, "slug": "mcp-x-00000000",
            "server_name": "mcp-x-00000000", "display_name": "X", "description": "d",
            "visibility": "private", "status": "active", "version": "1.0.0",
            "updated_at": _now(),
        }
        row.update(over)
        self.mcps[mid] = row
        return row


class FbFakeRegistry:
    """The seam the publisher hands each Contract-4 manifest to — captured here (the ARTIFACT)."""

    def __init__(self) -> None:
        self.registrations: list[dict] = []
        self.restrictions: list[dict] = []

    async def register(self, *, user_jwt, agent_id, name, manifest, is_update, trace_headers=None):
        self.registrations.append({"name": name, "manifest": manifest, "is_update": is_update})
        return {"name": name}

    async def mark_restricted(self, *, user_jwt, agent_id, name, reason,
                              default_access_mode="none", trace_headers=None):
        self.restrictions.append({"name": name, "default_access_mode": default_access_mode})


class FbFakeAdmin:
    async def get_flow(self, *, internal_host, admin_token, flow_id):
        return {"id": flow_id, "nodes": []}

    async def redeploy_flow(self, *, internal_host, admin_token, flow_id, flow):
        return True


def _now():
    from datetime import UTC, datetime
    return datetime.now(UTC)


class DummyPool:
    """A no-op stand-in for the psycopg AsyncConnectionPool the service lifespans create.

    Stubbed over ``db.pool.create_pool`` so NO real Postgres pool/libpq worker is ever opened
    (which, pointed at a dead port, wedges the Windows event-loop teardown). The harness never
    uses it: flow-bridge's db access is monkeypatched to the FakeStore, and the registry's
    request handlers read ``app.state.db_pool`` (the scripted double). Any stray query (e.g. the
    registry's fail-soft background health poll, which holds THIS pool) just gets empty results.
    """

    async def open(self, wait: bool = False) -> None:
        return None

    async def close(self) -> None:
        return None

    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        yield _DummyConn()


class _DummyConn:
    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self

    async def execute(self, sql: str, params: Any = None) -> _RgCursor:
        return _RgCursor([])

    def cursor(self, *, row_factory: Any = None) -> "_DummyCursorBuilder":
        return _DummyCursorBuilder()


class _DummyCursorBuilder:
    async def execute(self, sql: str, params: Any = None) -> _RgCursor:
        return _RgCursor([])


def install_flow_bridge_db_fakes(monkeypatch, st: FbFakeStore) -> None:
    """Patch the flow-bridge db.queries + db.pool + publisher preflight onto ``st`` (FakeStore).

    Mirrors tool-flow-bridge/tests/test_mcp_management.py's ``store`` fixture verbatim so the
    REAL publisher + REAL /m governed pipeline run against an in-memory table double (RLS
    emulated by tenant filtering). Node-RED itself is mocked separately at the HTTP transport.
    """

    async def in_tenant(pool, tenant_id, fn):
        return await fn(st)

    async def in_platform(pool, fn):
        return await fn(st)

    async def get_tool_by_snake_name(conn, snake_name):
        rows = [t for t in st.tools.values() if t["snake_name"] == snake_name]
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)[0] if rows else None

    async def create_tool(conn, tenant_id, **kw):
        return st.add_tool(tenant_id=tenant_id, **kw)

    async def update_tool(conn, tool_id, **kw):
        st.tools[tool_id].update({**kw, "status": "active"})
        return st.tools[tool_id]

    async def owned_tool_ids(conn, tool_ids):
        return {t for t in tool_ids if t in st.tools and st.tools[t]["tenant_id"] == TENANT}

    async def set_tool_status(conn, tool_id, status):
        st.tools[tool_id]["status"] = status

    async def list_tools(conn):
        return [t for t in st.tools.values() if t["status"] == "active"]

    async def list_tool_memberships(conn):
        out = []
        for (mid, tid) in st.links:
            m = st.mcps[mid]
            out.append({"tool_id": tid, "mcp_id": mid, "mcp_slug": m["slug"],
                        "mcp_server_name": m["server_name"], "mcp_status": m["status"]})
        return out

    async def get_mcp_by_slug(conn, slug):
        return next((m for m in st.mcps.values() if m["slug"] == slug), None)

    async def get_mcp_by_id(conn, mcp_id):
        return st.mcps.get(mcp_id)

    async def create_mcp(conn, tenant_id, **kw):
        return st.add_mcp(tenant_id=tenant_id, **kw)

    async def update_mcp(conn, mcp_id, **kw):
        st.mcps[mcp_id].update({**kw, "status": "active"})
        return st.mcps[mcp_id]

    async def set_mcp_status(conn, mcp_id, status):
        st.mcps[mcp_id]["status"] = status

    async def list_mcps(conn):
        return list(st.mcps.values())

    async def add_mcp_member(conn, mcp_id, tool_id, tenant_id):
        st.links.add((mcp_id, tool_id))

    async def set_mcp_members(conn, mcp_id, tenant_id, tool_ids):
        for (mid, tid) in list(st.links):
            if mid == mcp_id:
                st.links.discard((mid, tid))
        for tid in tool_ids:
            st.links.add((mcp_id, tid))

    async def get_mcp_members(conn, mcp_id):
        return [st.tools[tid] for (mid, tid) in st.links
                if mid == mcp_id and st.tools[tid]["status"] == "active"]

    async def get_mcp_with_members(conn, slug):
        m = next((x for x in st.mcps.values() if x["slug"] == slug), None)
        if m is None:
            return None
        members = [st.tools[tid] for (mid, tid) in st.links
                   if mid == m["mcp_id"] and st.tools[tid]["status"] == "active"]
        return m, members

    for name, fn in [
        ("get_tool_by_snake_name", get_tool_by_snake_name), ("create_tool", create_tool),
        ("update_tool", update_tool), ("owned_tool_ids", owned_tool_ids),
        ("set_tool_status", set_tool_status), ("list_tools", list_tools),
        ("list_tool_memberships", list_tool_memberships), ("get_mcp_by_slug", get_mcp_by_slug),
        ("get_mcp_by_id", get_mcp_by_id), ("create_mcp", create_mcp), ("update_mcp", update_mcp),
        ("set_mcp_status", set_mcp_status), ("list_mcps", list_mcps),
        ("add_mcp_member", add_mcp_member), ("set_mcp_members", set_mcp_members),
        ("get_mcp_members", get_mcp_members), ("get_mcp_with_members", get_mcp_with_members),
    ]:
        monkeypatch.setattr(fb_queries, name, fn)
    monkeypatch.setattr(fb_db_pool, "in_tenant", in_tenant)
    monkeypatch.setattr(fb_db_pool, "in_platform", in_platform)

    # Publish preflight (runtime + flow shape) — no Node-RED needed. The http_path is derived
    # from the flow id so each member routes to a distinct Node-RED endpoint (routing is
    # observable in the mocked Node-RED echo).
    async def ensure_runtime(pool, tenant_id, provisioner, settings):
        return {"runtime_id": "rt1", "internal_host": "http://nodered:1880",
                "admin_token_ref": "static:admin"}

    monkeypatch.setattr(fb_pub, "ensure_runtime", ensure_runtime)
    monkeypatch.setattr(fb_pub, "validate_flow_shape",
                        lambda flow: FlowShape("POST", f"/{flow['id']}"))


def nodered_mock_transport() -> httpx.MockTransport:
    """Stand in for the tenant's Node-RED HTTP-In endpoint (the ONLY faked flow HTTP).

    ``invoke_workflow`` (the REAL Node-RED adapter) posts to
    ``http://nodered:1880/flow/<flow_id>``; this echoes the path + args back so a caller can
    prove which member's binding a tools/call routed to.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            args = json.loads(request.content) if request.content else {}
        except ValueError:
            args = {}
        return httpx.Response(
            200,
            json={"echoed_from_nodered": True, "path": request.url.path, "args": args},
        )

    return httpx.MockTransport(handler)


# ══════════════════════════════════════════════════════════════════════════════════════
# registry fake — a param-aware, stateful psycopg pool double (the registry DB)
# ══════════════════════════════════════════════════════════════════════════════════════
class _RgCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict]:
        return self._rows

    async def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _RgConn:
    def __init__(self, store: "RegistryStore") -> None:
        self._store = store

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self

    async def execute(self, sql: str, params: Any = None) -> _RgCursor:
        return self._store.dispatch(sql, params)

    def cursor(self, *, row_factory: Any = None) -> "_RgCursorBuilder":
        return _RgCursorBuilder(self._store)


class _RgCursorBuilder:
    def __init__(self, store: "RegistryStore") -> None:
        self._store = store

    async def execute(self, sql: str, params: Any = None) -> _RgCursor:
        return self._store.dispatch(sql, params)


class RegistryStore:
    """A tiny in-memory Tool-Registry DB the REAL registry queries + endpoints run against.

    Faithful to the query SQL: register writes tools/tool_versions/tool_capabilities/tool_health;
    resolve + list read them back; the access endpoint reads restricted_tools + agent_tool_access
    (param-aware, so per-capability grants resolve correctly).
    """

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}                 # name -> tool row
        self.versions: dict[str, dict[str, dict]] = {}   # tool_id -> {version -> manifest}
        self.latest: dict[str, str] = {}                 # tool_id -> latest version
        self.caps: dict[str, list[dict]] = {}            # tool_id -> [{capability, required_scope}]
        self.restricted: dict[str, str] = {}             # tool_id -> default_access_mode
        self.access: dict[tuple[str, str], list[dict]] = {}  # (agent, server) -> [{tool_capability, access_mode}]
        self.last_tenant: str | None = None
        self._seq = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"reg-tool-{self._seq}"

    def grant_access(self, agent_id: str, server_name: str, capability: str | None, mode: str) -> None:
        self.access.setdefault((agent_id, server_name), []).append(
            {"tool_capability": capability, "access_mode": mode}
        )

    def dispatch(self, sql: str, params: Any) -> _RgCursor:
        norm = " ".join(sql.split())
        up = norm.upper()
        p = params or ()

        if "SET_CONFIG" in up:
            self.last_tenant = p[0] if p else ""
            return _RgCursor([])

        # ── writes (registration) ───────────────────────────────────────────────────
        if up.startswith("INSERT INTO TOOLS ("):
            name, version, visibility = p
            tool_id = self._new_id()
            tenant = self.last_tenant or None
            self.tools[name] = {
                "tool_id": tool_id, "name": name, "tenant_id": tenant,
                "status": "active", "latest_version": version, "visibility": visibility,
            }
            self.latest[tool_id] = version
            return _RgCursor([{**self.tools[name], "is_platform": tenant is None}])
        if up.startswith("INSERT INTO TOOL_VERSIONS"):
            tool_id, version, manifest = p
            manifest = getattr(manifest, "obj", manifest)  # unwrap psycopg Jsonb
            self.versions.setdefault(tool_id, {})[version] = manifest
            self.latest[tool_id] = version
            return _RgCursor([])
        if up.startswith("DELETE FROM TOOL_CAPABILITIES"):
            self.caps[p[0]] = []
            return _RgCursor([])
        if up.startswith("INSERT INTO TOOL_CAPABILITIES"):
            # tenant is inlined via current_setting(); params are (tool_id, capability, scope).
            tool_id, capability, required_scope = p
            self.caps.setdefault(tool_id, []).append(
                {"capability": capability, "required_scope": required_scope}
            )
            return _RgCursor([])
        if up.startswith("INSERT INTO TOOL_HEALTH"):
            return _RgCursor([])
        if up.startswith("UPDATE TOOLS SET"):
            return _RgCursor([])

        # ── reads (resolve / list / access) ─────────────────────────────────────────
        if "FROM TOOLS WHERE NAME = %S" in up:
            t = self.tools.get(p[0])
            return _RgCursor([{**t, "is_platform": t["tenant_id"] is None}] if t else [])
        if "FROM TOOL_VERSIONS" in up:
            tool_id = p[0]
            ver = self.latest.get(tool_id)
            if "AND VERSION = %S" in up and len(p) >= 2:
                ver = p[1]
            manifest = self.versions.get(tool_id, {}).get(ver)
            if manifest is None:
                return _RgCursor([])
            return _RgCursor([{"version": ver, "manifest": manifest,
                               "status": "active", "created_at": "now"}])
        if "FROM TOOL_CAPABILITIES WHERE TOOL_ID" in up:
            rows = sorted(self.caps.get(p[0], []), key=lambda c: c["capability"])
            return _RgCursor(rows)
        if "FROM TOOL_HEALTH WHERE TOOL_ID" in up:
            return _RgCursor([{"status": "active", "last_etag": None,
                               "consecutive_failures": 0, "last_polled": None}])
        if "FROM TOOLS.RESTRICTED_TOOLS" in up and "DEFAULT_ACCESS_MODE" in up:
            d = self.restricted.get(p[0])
            return _RgCursor([{"default_access_mode": d}] if d is not None else [])
        if "FROM TOOLS.RESTRICTED_TOOLS" in up:  # is_tool_restricted (SELECT 1)
            return _RgCursor([{"exists": 1}] if p[0] in self.restricted else [])
        if "FROM TOOLS.AGENT_TOOL_ACCESS" in up:
            agent_id, server_name, capability = p[0], p[1], p[2]
            rows = self.access.get((agent_id, server_name), [])
            matches = [r for r in rows
                       if r["tool_capability"] == capability or r["tool_capability"] is None]
            matches.sort(key=lambda r: r["tool_capability"] is not None, reverse=True)
            return _RgCursor([matches[0]] if matches else [])
        if "FROM TOOLS T" in up:
            # list_visible_tools carries ORDER BY t.name; the background health poll's
            # list_pollable_tools does not — it returns [] here (it uses the real deferred pool).
            if "ORDER BY T.NAME" in up:
                rows = [{**t, "is_platform": t["tenant_id"] is None} for t in self.tools.values()]
                if "VISIBILITY = ANY(%S)" in up:
                    wanted = set(p[0])
                    rows = [r for r in rows if r["visibility"] in wanted]
                return _RgCursor(rows)
            return _RgCursor([])

        return _RgCursor([])


class RegistryPool:
    def __init__(self, store: RegistryStore) -> None:
        self._store = store

    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        yield _RgConn(self._store)


# ══════════════════════════════════════════════════════════════════════════════════════
# xAgent seam — a fake service-token provider + a scripted LLM
# ══════════════════════════════════════════════════════════════════════════════════════
class FakeTokenProvider:
    """Contract-12 service-token provider double — the target apps override auth, so any string
    is accepted; only the header presence matters."""

    async def get_token(self, *, on_behalf_of: str | None = None) -> str:
        return "svc.e2e.jwt"


class FakeLlms:
    """A scripted LLM: returns the next canned completion per chat() call and records what tool
    schemas were offered (the LLM is out of scope — it only picks tool calls to drive the loop)."""

    def __init__(self, completions: list[ChatCompletion]) -> None:
        self.completions = list(completions)
        self.offered: list[list[dict[str, Any]]] = []

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.offered.append(kw.get("tools") or [])
        return self.completions.pop(0)


def _completion(*, content: str | None = None,
                tool_calls: list[ToolCall] | None = None) -> ChatCompletion:
    return ChatCompletion(
        content=content,
        finish_reason="tool_calls" if tool_calls else "stop",
        model="smart",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
        tool_calls=tool_calls or [],
        raw={},
    )


def _ctx(agent: AgentRuntime) -> PipelineContext:
    return PipelineContext(
        principal=AxPrincipal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"],
                              raw_token="agent.jwt"),
        inbound_agent_jwt="agent.jwt", trace_id=TRACE_ID, request_id="req-1",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT, trace_id=TRACE_ID,
                     status="running", input={"message": "go"}),
        agent=agent, prompt_text="go", messages=[{"role": "user", "content": "go"}],
        steps=StepBuffer(), pool=None, started_monotonic=time.monotonic(), cost_budget_usd=None,
    )


# ══════════════════════════════════════════════════════════════════════════════════════
# app builders
# ══════════════════════════════════════════════════════════════════════════════════════
async def _boot(app, stack: contextlib.AsyncExitStack):
    await stack.enter_async_context(LifespanManager(app, startup_timeout=20))
    return app


async def build_flow_bridge_app(stack, store: FbFakeStore, registry: FbFakeRegistry):
    """Real flow-bridge ASGI app: auth overridden, DB monkeypatched to ``store``, publisher's
    registry swapped for the capturing ``registry``, Node-RED mocked at app.state.http_client,
    and app.state.registry=None (invoke access fails OPEN — xAgent is the fail-closed gate)."""
    app = fb_create_app()
    app.dependency_overrides[fb_require_principal] = lambda: FbPrincipal(
        tenant_id=TENANT, agent_id=AGENT, scopes=list(FB_ADMIN_SCOPES), principal_type="agent"
    )
    await _boot(app, stack)
    app.state.valkey = FbFakeValkey()
    app.state.registry = None                               # invoke access -> fail open (allow)
    app.state.publisher._registry = registry               # capture registrations (the artifact)
    app.state.publisher._admin = FbFakeAdmin()
    # Replace the lifespan's real httpx client with the Node-RED mock (the ONLY faked flow HTTP).
    old_http = app.state.http_client
    app.state.http_client = httpx.AsyncClient(transport=nodered_mock_transport())
    stack.push_async_callback(app.state.http_client.aclose)
    with contextlib.suppress(Exception):
        await old_http.aclose()
    return app


async def build_registry_app(stack, store: RegistryStore):
    """Real registry ASGI app: auth overridden to an admin agent principal, DB = scripted double,
    http_client=None so the eager health poll is a no-op."""
    app = rg_create_app()
    app.dependency_overrides[rg_require_principal] = lambda: RgPrincipal(
        tenant_id=TENANT, agent_id=AGENT, scopes=["tool:admin", "tool:invoke"],
        principal_type="agent",
    )
    await _boot(app, stack)
    app.state.db_pool = RegistryPool(store)
    app.state.http_client = None
    app.state.valkey = None
    return app


# ══════════════════════════════════════════════════════════════════════════════════════
# the end-to-end test
# ══════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_private_protected_cross_service_chain(monkeypatch) -> None:
    fb_store = FbFakeStore()
    fb_registry = FbFakeRegistry()
    rg_store = RegistryStore()
    install_flow_bridge_db_fakes(monkeypatch, fb_store)
    # No real psycopg pool for either service lifespan — fully hermetic, no sockets.
    monkeypatch.setattr(fb_db_pool, "create_pool", lambda *a, **k: DummyPool())
    monkeypatch.setattr(rg_db_pool, "create_pool", lambda *a, **k: DummyPool())

    async with contextlib.AsyncExitStack() as stack:
        fb_app = await build_flow_bridge_app(stack, fb_store, fb_registry)
        rg_app = await build_registry_app(stack, rg_store)

        # One httpx client per ASGI app (ASGITransport routes by PATH; host is ignored).
        fb_http = await stack.enter_async_context(
            AsyncClient(transport=ASGITransport(app=fb_app), base_url="http://tool-flow-bridge:8080")
        )
        rg_http = await stack.enter_async_context(
            AsyncClient(transport=ASGITransport(app=rg_app), base_url="http://tool-registry:8080")
        )

        # The REAL xAgent registry + MCP clients, pointed at the ASGI transports. resolve_tool +
        # get_tool_access hit the registry app; invoke_mcp hits the flow-bridge /m wire.
        ax_settings = ax_get_settings()
        tokens = FakeTokenProvider()
        registry_client = RegistryClient(ax_settings, tokens, client=rg_http)
        mcp_client = McpClient(ax_settings, tokens, client=fb_http)
        ax_deps.set_enhancement_clients(registry_client=registry_client, mcp_client=mcp_client)
        try:
            await _scenario_a_single_private_tool(fb_http, rg_http, fb_registry, rg_store,
                                                  registry_client, mcp_client)
            await _scenario_b_multi_tool_protected_mcp(fb_http, rg_http, fb_registry, rg_store,
                                                       registry_client, mcp_client)
            await _assert_marketplace_visibility_sections(rg_http)
        finally:
            ax_deps.set_enhancement_clients()
            ax_deps.set_clients(guardrails_client=None, llms_client=None)


# ── Scenario A — a single PRIVATE atomic tool (auto-singleton MCP) ───────────────────────
async def _scenario_a_single_private_tool(fb_http, rg_http, fb_registry, rg_store,
                                          registry_client, mcp_client) -> None:
    fb_registry.registrations.clear()

    # hop 1 — flow-bridge create the atomic tool (auto-singleton, private) + publish.
    resp = await fb_http.post("/v1/tools", json={
        "node_red_flow_id": "flow-notify", "title": "Notify", "description": "send a notification",
        "access_mode": "automated", "visibility": "private",
        "input_params": [{"name": "msg", "type": "string", "required": True}],
    })
    assert resp.status_code == 201, resp.text
    created = resp.json()
    slug = created["mcp_slug"]                    # notify-00000000
    server_name = created["server_name"]          # tool-notify-00000000
    assert server_name == f"tool-{slug}"

    # The captured Contract-4 manifest the publisher registered — the SHARED ARTIFACT.
    assert len(fb_registry.registrations) == 1
    artifact = fb_registry.registrations[0]["manifest"]
    assert artifact["name"] == server_name
    assert artifact["visibility"] == "private"
    assert [t["name"] for t in artifact["tools"]] == ["notify"]
    assert artifact["base_url"].endswith(f"/m/{slug}")
    assert artifact["mcp"]["endpoint"] == "/mcp"
    fb_tools_names = [t["name"] for t in artifact["tools"]]
    fb_input_schema = artifact["tools"][0]["input_schema"]
    fb_required_scopes = artifact["required_scopes"]

    # hop 2 — registry register the captured manifest, then list + resolve.
    reg = await rg_http.post("/v1/tools", json=artifact)
    assert reg.status_code == 201, reg.text
    assert reg.json()["name"] == server_name
    assert reg.json()["visibility"] == "private"        # stored visibility == emitted visibility

    view = (await rg_http.get(f"/v1/tools/{server_name}")).json()
    assert view["name"] == server_name                  # server_name: registry == flow-bridge
    assert view["visibility"] == "private"
    assert view["invoke_url"] == artifact["base_url"].rstrip("/")   # /m/<slug>
    assert view["capabilities"] == ["notify"]           # capabilities line up with tools[]
    assert view["required_scopes"] == fb_required_scopes
    assert [t["name"] for t in view["manifest"]["tools"]] == fb_tools_names
    assert view["manifest"]["tools"][0]["input_schema"] == fb_input_schema

    # hop 3 — the REAL xAgent tool loop resolves from the registry, offers notify, invokes it
    # through the flow-bridge /m wire; the mocked Node-RED result flows back.
    resolution = await registry_client.resolve_tool(server_name, None, agent_jwt="agent.jwt",
                                                    on_behalf_of=AGENT)
    assert resolution.name == server_name               # server_name: xAgent == registry == flow-bridge
    assert resolution.invoke_url == view["invoke_url"]
    assert ToolLoopStage._mcp_endpoint_of(resolution.invoke_url, resolution.manifest) == \
        f"{view['invoke_url']}/mcp"                      # {invoke_url}{mcp.endpoint} = /m/<slug>/mcp

    llms = FakeLlms([
        _completion(tool_calls=[ToolCall(id="c1", name="notify", arguments={"msg": "hi"})]),
        _completion(content="notified"),
    ])
    ax_deps.set_clients(guardrails_client=None, llms_client=llms)
    agent = AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
                         llm_model="smart", allowed_tools=[server_name])
    ctx = _ctx(agent)
    await ToolLoopStage().run(ctx)

    # The single allowed_tools entry offered exactly the member tool notify.
    assert [t["function"]["name"] for t in llms.offered[0]] == ["notify"]
    assert ctx.terminal_error is None
    # The invocation actually ran the governed pipeline against the flow-bridge /m wire and hit
    # (mocked) Node-RED at the member's binding.
    assert len(ctx.tool_results) == 1
    result = ctx.tool_results[0]["result"]
    assert result["echoed_from_nodered"] is True
    assert result["path"] == "/flow/flow-notify"        # routed to notify's Node-RED binding
    assert result["args"] == {"msg": "hi"}
    tool_steps = [s for s in ctx.steps.steps if s.step_type == STEP_TYPE_TOOL_CALL]
    assert [s.status for s in tool_steps] == ["passed"]

    # What /m actually serves matches the artifact (server_name + tools[] + inputSchema).
    init = await _jsonrpc(fb_http, slug, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == server_name    # /m serves the same server_name
    listed = await _jsonrpc(fb_http, slug, "tools/list", {})
    assert [t["name"] for t in listed["result"]["tools"]] == fb_tools_names
    assert listed["result"]["tools"][0]["inputSchema"] == fb_input_schema


# ── Scenario B — a PROTECTED MULTI-tool MCP: routing by name + per-capability deny ───────
async def _scenario_b_multi_tool_protected_mcp(fb_http, rg_http, fb_registry, rg_store,
                                               registry_client, mcp_client) -> None:
    # hop 1a — create two atomic tools (each auto-singleton), then bundle them into ONE MCP.
    alpha = (await fb_http.post("/v1/tools", json={
        "node_red_flow_id": "flow-alpha", "title": "Alpha", "description": "the alpha member",
        "access_mode": "automated", "visibility": "protected",
        "input_params": [{"name": "q", "type": "string"}],
    })).json()
    beta = (await fb_http.post("/v1/tools", json={
        "node_red_flow_id": "flow-beta", "title": "Beta", "description": "the beta member",
        "access_mode": "automated", "visibility": "protected",
        "input_params": [{"name": "q", "type": "string"}],
    })).json()

    # hop 1b — create the aggregating MCP over BOTH tools (protected), capturing its manifest.
    fb_registry.registrations.clear()
    mcp_resp = await fb_http.post("/v1/mcps", json={
        "display_name": "Suite", "description": "a two-tool suite", "visibility": "protected",
        "tool_ids": [alpha["tool_id"], beta["tool_id"]],
    })
    assert mcp_resp.status_code == 201, mcp_resp.text
    mcp_view = mcp_resp.json()
    slug = mcp_view["slug"]                       # mcp-suite-00000000
    server_name = mcp_view["server_name"]         # mcp-suite-00000000 (server_name == slug)

    assert len(fb_registry.registrations) == 1
    artifact = fb_registry.registrations[0]["manifest"]
    assert artifact["name"] == server_name
    assert artifact["visibility"] == "protected"
    fb_tools_names = sorted(t["name"] for t in artifact["tools"])
    assert fb_tools_names == ["alpha", "beta"]    # aggregating manifest lists BOTH members
    fb_schema_by_name = {t["name"]: t["input_schema"] for t in artifact["tools"]}

    # hop 2 — register + resolve the multi-tool manifest.
    reg = await rg_http.post("/v1/tools", json=artifact)
    assert reg.status_code == 201, reg.text
    assert reg.json()["visibility"] == "protected"
    view = (await rg_http.get(f"/v1/tools/{server_name}")).json()
    assert view["name"] == server_name
    assert view["visibility"] == "protected"
    assert sorted(view["capabilities"]) == ["alpha", "beta"]   # per-member capabilities
    assert view["invoke_url"] == artifact["base_url"].rstrip("/")

    # Per-capability access in the registry: mark the server restricted (default none) and grant
    # ONLY alpha=automated -> alpha resolves automated, beta falls to the none default.
    reg_tool_id = rg_store.tools[server_name]["tool_id"]
    rg_store.restricted[reg_tool_id] = "none"
    rg_store.grant_access(AGENT, server_name, "alpha", "automated")
    assert await registry_client.get_tool_access(server_name, capability="alpha",
                                                  agent_jwt="agent.jwt", on_behalf_of=AGENT) == "automated"
    assert await registry_client.get_tool_access(server_name, capability="beta",
                                                  agent_jwt="agent.jwt", on_behalf_of=AGENT) == "none"

    # hop 3 — the model requests BOTH members in one turn. alpha (automated) invokes through /m;
    # beta (none) is denied fail-closed BEFORE any invoke. Routing is by member name.
    llms = FakeLlms([
        _completion(tool_calls=[
            ToolCall(id="a", name="alpha", arguments={"q": "x"}),
            ToolCall(id="b", name="beta", arguments={"q": "y"}),
        ]),
        _completion(content="done"),
    ])
    ax_deps.set_clients(guardrails_client=None, llms_client=llms)
    agent = AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="B", system_prompt="s",
                         llm_model="smart", allowed_tools=[server_name])
    ctx = _ctx(agent)
    await ToolLoopStage().run(ctx)

    # ONE server entry -> BOTH members offered to the LLM under their own names.
    assert sorted(t["function"]["name"] for t in llms.offered[0]) == ["alpha", "beta"]
    assert ctx.terminal_error is None

    # alpha invoked, routed to alpha's Node-RED binding; beta denied (never invoked).
    assert [r["tool"] for r in ctx.tool_results] == ["alpha"]
    alpha_result = ctx.tool_results[0]["result"]
    assert alpha_result["path"] == "/flow/flow-alpha"      # routed to alpha, not beta
    assert alpha_result["args"] == {"q": "x"}
    steps = {s.output["tool"]: s for s in ctx.steps.steps if s.step_type == STEP_TYPE_TOOL_CALL}
    assert steps["alpha"].status == "passed"
    assert steps["beta"].status == "failed"
    assert steps["beta"].output["error"] == "tool_access_denied"

    # What /m serves matches the artifact (server_name + both members' names + inputSchemas).
    init = await _jsonrpc(fb_http, slug, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == server_name
    listed = await _jsonrpc(fb_http, slug, "tools/list", {})
    served = {t["name"]: t["inputSchema"] for t in listed["result"]["tools"]}
    assert sorted(served) == ["alpha", "beta"]
    assert served == fb_schema_by_name

    # Route-by-name at the /m wire directly: calling beta hits beta's binding (proving the router
    # keys off the member name, independent of the access decision above).
    beta_call = await _jsonrpc(fb_http, slug, "tools/call", {"name": "beta", "arguments": {"q": "z"}})
    assert beta_call["result"]["structuredContent"]["path"] == "/flow/flow-beta"
    unknown = await _jsonrpc(fb_http, slug, "tools/call", {"name": "ghost", "arguments": {}})
    assert unknown["error"]["code"] == -32602            # INVALID_PARAMS for an unknown member


# ── Marketplace: the two tools section by visibility (private vs protected) ──────────────
async def _assert_marketplace_visibility_sections(rg_http) -> None:
    listing = (await rg_http.get("/v1/tools")).json()["data"]
    by_name = {d["name"]: d for d in listing}
    assert by_name["tool-notify-00000000"]["visibility"] == "private"
    assert by_name["mcp-suite-00000000"]["visibility"] == "protected"

    protected = (await rg_http.get("/v1/tools", params={"visibility": "protected"})).json()["data"]
    names = {d["name"] for d in protected}
    assert names == {"mcp-suite-00000000"}               # filter narrows to the protected section

    private = (await rg_http.get("/v1/tools", params={"visibility": "private"})).json()["data"]
    assert {d["name"] for d in private} == {"tool-notify-00000000"}


# ── helper: post a single JSON-RPC message to the flow-bridge /m wire (test-side assertions) ──
async def _jsonrpc(fb_http, slug: str, method: str, params: dict) -> dict:
    resp = await fb_http.post(
        f"/m/{slug}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()
