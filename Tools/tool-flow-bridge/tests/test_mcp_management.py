"""Phase-2 control plane: /v1/tools + /v1/mcps CRUD, publish, unpublish, promote.

Exercises the REAL publisher logic against an in-memory fake of the ``queries`` module (the new
source-of-truth model tools+mcps+mcp_tools), a fake registry that records registrations, and a
fake Node-RED admin. Proves the Phase-1 findings are closed end to end:

* create-tool auto-creates a singleton MCP (server_name ``tool-<slug>``) and registers it (#4).
* create-MCP ownership validation rejects a foreign tool_id (#3, app layer).
* a membership update re-registers the aggregating manifest (#4).
* unpublish retires the mirror so the tool is no longer invokable via ``/m`` (#4 case c).
* promote registers under the platform namespace (visibility=public, author=platform) (GUARD #8).
* the idempotency scope is shared across the ``/w`` and ``/m`` wires (#2).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conftest import TEST_TENANT, make_principal
from tool_flow_bridge.api import mcp as mcp_api
from tool_flow_bridge.core.errors import ApiError, ErrorCode
from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries
from tool_flow_bridge.services import mcp_protocol
from tool_flow_bridge.services import publisher as pub
from tool_flow_bridge.services.nodered_admin import FlowShape

ADMIN = ["tool:invoke", "tool:admin", "tenant:admin", "platform:admin"]


class FakeStore:
    """Minimal in-memory stand-in for the flow_tools tables (RLS emulated by tenant filtering)."""

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
            "tool_id": tid, "tenant_id": TEST_TENANT, "snake_name": "t", "display_name": "T",
            "description": "d", "input_schema": {"type": "object", "properties": {}},
            "output_schema": None, "node_red_flow_id": "f1", "http_method": "POST",
            "http_path": "/x", "runtime_id": "rt1", "visibility": "private",
            "access_mode": "automated", "version": "1.0.0", "status": "active",
            "updated_at": datetime.now(UTC), "internal_host": "http://nodered:1880",
            "http_node_root": "/flow", "invoke_secret_ref": "static:invoke",
        }
        row.update(over)
        self.tools[tid] = row
        return row

    def add_mcp(self, **over) -> dict:
        mid = over.get("mcp_id") or self._id("mid")
        row = {
            "mcp_id": mid, "tenant_id": TEST_TENANT, "slug": "mcp-x-aabbccdd",
            "server_name": "mcp-x-aabbccdd", "display_name": "X", "description": "d",
            "visibility": "private", "status": "active", "version": "1.0.0",
            "updated_at": datetime.now(UTC),
        }
        row.update(over)
        self.mcps[mid] = row
        return row


class FakeRegistry:
    def __init__(self) -> None:
        self.registrations: list[dict] = []
        self.platform_registrations: list[dict] = []
        self.retirements: list[str] = []
        self.restrictions: list[dict] = []

    async def register(self, *, user_jwt, agent_id, name, manifest, is_update, trace_headers=None):
        self.registrations.append({"name": name, "manifest": manifest, "is_update": is_update})
        return {"name": name}

    async def register_platform(self, *, user_jwt, agent_id, name, manifest, trace_headers=None):
        self.platform_registrations.append({"name": name, "manifest": manifest})
        return {"name": name, "owner": "platform"}

    async def retire(self, *, user_jwt, agent_id, name, trace_headers=None):
        self.retirements.append(name)
        return {"name": name, "status": "retired"}

    async def mark_restricted(self, *, user_jwt, agent_id, name, reason, default_access_mode="none",
                              trace_headers=None):
        self.restrictions.append({"name": name, "default_access_mode": default_access_mode})


class FakeAdmin:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self._seq = 0

    async def get_flow(self, *, internal_host, admin_token, flow_id):
        return {"id": flow_id, "label": flow_id, "nodes": []}

    async def create_flow(self, *, internal_host, admin_token, flow):
        self._seq += 1
        new_id = f"platform-flow-{self._seq}"
        self.created.append({"host": internal_host, "flow": flow, "id": new_id})
        return new_id

    async def delete_flow(self, *, internal_host, admin_token, flow_id):
        self.deleted.append(flow_id)
        return True

    async def redeploy_flow(self, *, internal_host, admin_token, flow_id, flow):
        return True


@pytest.fixture
def store(monkeypatch):
    st = FakeStore()

    async def in_tenant(pool, tenant_id, fn):
        return await fn(st)

    async def in_platform(pool, fn):
        return await fn(st)

    # ── tools ──
    async def get_tool_by_snake_name(conn, snake_name):
        rows = [t for t in st.tools.values() if t["snake_name"] == snake_name]
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)[0] if rows else None

    async def create_tool(conn, tenant_id, **kw):
        return st.add_tool(tenant_id=tenant_id, **kw)

    async def update_tool(conn, tool_id, **kw):
        st.tools[tool_id].update({**kw, "status": "active"})
        return st.tools[tool_id]

    async def owned_tool_ids(conn, tool_ids):
        return {t for t in tool_ids if t in st.tools and st.tools[t]["tenant_id"] == TEST_TENANT}

    async def set_tool_status(conn, tool_id, status):
        st.tools[tool_id]["status"] = status

    async def repoint_tool_runtime(conn, tool_id, *, runtime_id, node_red_flow_id):
        st.tools[tool_id]["runtime_id"] = runtime_id
        st.tools[tool_id]["node_red_flow_id"] = node_red_flow_id

    async def set_tools_visibility(conn, tool_ids, visibility):
        for tid in tool_ids:
            if tid in st.tools:
                st.tools[tid]["visibility"] = visibility

    async def list_tools(conn):
        # Mirror the real query's WHERE status='active' — the rail lists ACTIVE tools only
        # (a retired/unpublished tool must drop off), like get_mcp_members below.
        return [t for t in st.tools.values() if t["status"] == "active"]

    async def list_tool_memberships(conn):
        out = []
        for (mid, tid) in st.links:
            m = st.mcps[mid]
            out.append({"tool_id": tid, "mcp_id": mid, "mcp_slug": m["slug"],
                        "mcp_server_name": m["server_name"], "mcp_status": m["status"]})
        return out

    # ── mcps ──
    async def get_mcp_by_slug(conn, slug):
        return next((m for m in st.mcps.values() if m["slug"] == slug), None)

    async def get_mcp_by_id(conn, mcp_id):
        return st.mcps.get(mcp_id)

    async def create_mcp(conn, tenant_id, **kw):
        return st.add_mcp(tenant_id=tenant_id, **kw)

    async def update_mcp(conn, mcp_id, **kw):
        st.mcps[mcp_id].update({**kw, "status": "active"})
        return st.mcps[mcp_id]

    async def promote_mcp_row(conn, mcp_id, **kw):
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

    async def get_member_tool_ids(conn, mcp_id):
        return [tid for (mid, tid) in st.links if mid == mcp_id]

    async def exclusive_member_tool_ids(conn, mcp_id):
        mine = {tid for (mid, tid) in st.links if mid == mcp_id}
        # Only an ACTIVE sibling MCP keeps a tool reachable (mirrors the real SQL: a link to an
        # already-retired MCP does not count).
        elsewhere = {tid for (mid, tid) in st.links
                     if mid != mcp_id and st.mcps.get(mid, {}).get("status") == "active"}
        return [tid for tid in mine if tid not in elsewhere]

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
        ("set_tool_status", set_tool_status), ("repoint_tool_runtime", repoint_tool_runtime),
        ("set_tools_visibility", set_tools_visibility),
        ("list_tools", list_tools),
        ("list_tool_memberships", list_tool_memberships), ("get_mcp_by_slug", get_mcp_by_slug),
        ("get_mcp_by_id", get_mcp_by_id), ("create_mcp", create_mcp), ("update_mcp", update_mcp),
        ("promote_mcp_row", promote_mcp_row), ("set_mcp_status", set_mcp_status),
        ("list_mcps", list_mcps), ("add_mcp_member", add_mcp_member),
        ("set_mcp_members", set_mcp_members), ("get_mcp_members", get_mcp_members),
        ("get_member_tool_ids", get_member_tool_ids),
        ("exclusive_member_tool_ids", exclusive_member_tool_ids),
        ("get_mcp_with_members", get_mcp_with_members),
    ]:
        monkeypatch.setattr(queries, name, fn)
    monkeypatch.setattr(db_pool, "in_tenant", in_tenant)
    monkeypatch.setattr(db_pool, "in_platform", in_platform)

    # Publish preflight (runtime + flow shape) — no Node-RED needed.
    async def ensure_runtime(pool, tenant_id, provisioner, settings):
        return {"runtime_id": "rt1", "internal_host": "http://nodered:1880",
                "admin_token_ref": "static:admin"}

    async def ensure_platform_runtime(pool, provisioner, settings):
        return {"runtime_id": "platform-rt", "internal_host": "http://nodered-platform:1880",
                "admin_token_ref": "static:platform-admin"}

    monkeypatch.setattr(pub, "ensure_runtime", ensure_runtime)
    monkeypatch.setattr(pub, "ensure_platform_runtime", ensure_platform_runtime)
    monkeypatch.setattr(pub, "validate_flow_shape", lambda flow: FlowShape("POST", "/x"))
    return st


@pytest.fixture
def registry():
    return FakeRegistry()


async def _client(make_client, registry, scopes=None):
    ac = await make_client(principal=make_principal(scopes if scopes is not None else ADMIN))
    ac.app.state.publisher._registry = registry
    ac.app.state.publisher._admin = FakeAdmin()
    return ac


def _tool_body(**over) -> dict:
    body = {"node_red_flow_id": "flow-1", "title": "Adder", "description": "adds",
            "access_mode": "automated", "input_params": [{"name": "a", "type": "integer"}]}
    body.update(over)
    return body


# ── create-tool + auto-singleton (finding #4) ────────────────────────────────────
async def test_create_tool_auto_singleton(make_client, store, registry) -> None:
    ac = await _client(make_client, registry)
    resp = await ac.post("/v1/tools", json=_tool_body())
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_update"] is False
    # The singleton MCP preserves the registry key tool-<slug>; base wire is /m/<slug>.
    assert body["server_name"] == f"tool-{body['mcp_slug']}"
    assert body["mcps"][0]["server_name"] == f"tool-{body['mcp_slug']}"
    assert len(registry.registrations) == 1
    reg = registry.registrations[0]
    assert reg["name"] == f"tool-{body['mcp_slug']}"
    assert reg["manifest"]["base_url"].endswith(f"/m/{body['mcp_slug']}")
    assert [t["name"] for t in reg["manifest"]["tools"]] == ["adder"]
    # GET /v1/tools shows the tool + its singleton membership.
    listing = await ac.get("/v1/tools")
    data = listing.json()["data"]
    assert data[0]["snake_name"] == "adder"
    assert data[0]["mcps"][0]["slug"] == body["mcp_slug"]


# ── the "Published tools" rail lists ACTIVE tools only (finding #8) ───────────────
async def test_list_tools_excludes_retired(make_client, store, registry) -> None:
    """A retired (unpublished) tool must NOT appear on GET /v1/tools — the rail is active-only,
    matching the member semantics used everywhere else."""
    store.add_tool(snake_name="alive", status="active")
    store.add_tool(snake_name="gone", status="retired")
    ac = await _client(make_client, registry)
    data = (await ac.get("/v1/tools")).json()["data"]
    names = {t["snake_name"] for t in data}
    assert "alive" in names
    assert "gone" not in names


class _CaptureCursor:
    """Records the SQL executed so the real query text can be asserted (no live DB in the suite)."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.sql: str | None = None

    async def __aenter__(self) -> _CaptureCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: object = None) -> _CaptureCursor:
        self.sql = sql
        return self

    async def fetchall(self) -> list[dict]:
        return self._rows


class _CaptureConn:
    def __init__(self, rows: list[dict]) -> None:
        self.cursor_obj = _CaptureCursor(rows)

    def cursor(self, *, row_factory: object = None) -> _CaptureCursor:
        return self.cursor_obj


async def test_list_tools_query_filters_active_status() -> None:
    """The real queries.list_tools SQL restricts to status='active' (guards the fix directly)."""
    conn = _CaptureConn([{"tool_id": "t1", "snake_name": "alive", "status": "active"}])
    rows = await queries.list_tools(conn)  # type: ignore[arg-type]
    assert rows[0]["snake_name"] == "alive"
    normalized = " ".join((conn.cursor_obj.sql or "").split())
    assert "WHERE status = 'active'" in normalized


# ── create-MCP ownership validation rejects a foreign tool_id (finding #3) ────────
async def test_create_mcp_rejects_foreign_tool(make_client, store, registry) -> None:
    owned = store.add_tool(snake_name="mine")
    ac = await _client(make_client, registry)
    resp = await ac.post(
        "/v1/mcps",
        json={"display_name": "Bundle", "description": "d",
              "tool_ids": [owned["tool_id"], "tid-foreign"]},
    )
    assert resp.status_code == 403
    assert "tid-foreign" in resp.json()["error"]["details"]["unauthorized_tool_ids"]
    assert registry.registrations == []  # never registered a bad MCP


async def test_create_mcp_registers_owned_tools(make_client, store, registry) -> None:
    t1 = store.add_tool(snake_name="one")
    t2 = store.add_tool(snake_name="two")
    ac = await _client(make_client, registry)
    resp = await ac.post(
        "/v1/mcps",
        json={"display_name": "Bundle", "description": "d",
              "tool_ids": [t1["tool_id"], t2["tool_id"]]},
    )
    assert resp.status_code == 201
    assert len(registry.registrations) == 1
    assert sorted(t["name"] for t in registry.registrations[0]["manifest"]["tools"]) == ["one", "two"]
    # The MCP view surfaces each member's DEFAULT access mode so the agent picker can seed
    # allowed-vs-greyed members without a second round-trip (frontend fix #3).
    members = resp.json()["tools"]
    assert {m["snake_name"]: m["access_mode"] for m in members} == {"one": "automated", "two": "automated"}


# ── membership update re-registers (finding #4) ──────────────────────────────────
async def test_membership_update_reregisters(make_client, store, registry) -> None:
    t1 = store.add_tool(snake_name="one")
    t2 = store.add_tool(snake_name="two")
    ac = await _client(make_client, registry)
    created = await ac.post(
        "/v1/mcps",
        json={"display_name": "Bundle", "description": "d", "tool_ids": [t1["tool_id"]]},
    )
    mcp_id = created.json()["mcp_id"]
    assert len(registry.registrations[-1]["manifest"]["tools"]) == 1
    updated = await ac.put(f"/v1/mcps/{mcp_id}",
                           json={"tool_ids": [t1["tool_id"], t2["tool_id"]]})
    assert updated.status_code == 200
    assert len(registry.registrations) == 2  # re-registered on membership change
    assert len(registry.registrations[-1]["manifest"]["tools"]) == 2


# ── unpublish retires the mirror so /m no longer invokes (finding #4 case c) ──────
async def test_unpublish_retires_mirror(make_client, store, registry, monkeypatch) -> None:
    async def fake_access(*_a, **_k):
        return "automated"

    invoked = {"n": 0}

    async def fake_invoke(client, **kw):
        invoked["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(mcp_api, "_resolve_tool_access", fake_access)
    monkeypatch.setattr(mcp_api, "invoke_workflow", fake_invoke)

    ac = await _client(make_client, registry)
    created = (await ac.post("/v1/tools", json=_tool_body())).json()
    slug, mcp_id = created["mcp_slug"], created["mcps"][0]["mcp_id"]

    # Invokable via /m before unpublish.
    ok = await ac.post(f"/m/{slug}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": "adder", "arguments": {"a": 1}}})
    assert ok.json()["result"]["isError"] is False

    deleted = await ac.delete(f"/v1/mcps/{mcp_id}")
    assert deleted.status_code == 200
    assert deleted.json()["retired_tools"] == [created["tool_id"]]
    assert store.mcps[mcp_id]["status"] == "retired"

    # No longer resolvable/invokable via /m (mcp status retired -> _load_mcp returns None).
    invoked["n"] = 0
    gone = await ac.post(f"/m/{slug}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                 "params": {"name": "adder", "arguments": {"a": 1}}})
    assert gone.json()["error"]["code"] == mcp_protocol.INVALID_PARAMS
    assert invoked["n"] == 0


# ── exclusive-retire respects sibling MCP STATUS (finding D) ──────────────────────
async def test_unpublish_retires_only_after_last_active_mcp(make_client, store, registry) -> None:
    """A tool shared by two MCPs survives when one is unpublished, and is retired only when its
    LAST active MCP is unpublished — a membership in an already-retired sibling must not keep it
    alive (else it orphans as status='active' with no MCP exposing it)."""
    shared = store.add_tool(snake_name="shared")
    ac = await _client(make_client, registry)
    a = (await ac.post("/v1/mcps", json={"display_name": "A", "description": "d",
                                         "tool_ids": [shared["tool_id"]]})).json()
    b = (await ac.post("/v1/mcps", json={"display_name": "B", "description": "d",
                                         "tool_ids": [shared["tool_id"]]})).json()

    # Unpublish B: the tool is still exposed by ACTIVE MCP A -> it survives.
    d1 = await ac.delete(f"/v1/mcps/{b['mcp_id']}")
    assert d1.status_code == 200
    assert d1.json()["retired_tools"] == []
    assert store.tools[shared["tool_id"]]["status"] == "active"

    # Unpublish A (last active MCP; the only sibling B is already RETIRED) -> tool retired.
    d2 = await ac.delete(f"/v1/mcps/{a['mcp_id']}")
    assert d2.status_code == 200
    assert d2.json()["retired_tools"] == [shared["tool_id"]]
    assert store.tools[shared["tool_id"]]["status"] == "retired"


# ── promote: re-home + platform register + retire old (GUARD #8, Phase 5) ─────────
async def test_promote_registers_platform(make_client, store, registry) -> None:
    # TEST_TENANT's tenant8 is 00000000, so the platform form strips '-00000000'.
    mcp = store.add_mcp(slug="mcp-math-00000000", server_name="mcp-math-00000000",
                        display_name="Math", version="1.2.0")
    store.add_tool(tool_id="tid-add", snake_name="add", node_red_flow_id="f-add", runtime_id="rt1")
    store.links.add((mcp["mcp_id"], "tid-add"))

    ac = await _client(make_client, registry)
    admin = ac.app.state.publisher._admin
    resp = await ac.post(f"/v1/mcps/{mcp['mcp_id']}/promote")
    assert resp.status_code == 200
    body = resp.json()
    assert body["visibility"] == "public"
    assert body["slug"] == "mcp-math"  # tenant8 stripped
    assert body["registry_status"] == "registered"
    assert body["runtime_rehomed"] is True  # Phase 5: flows re-homed onto the platform runtime
    # Registered via the PLATFORM path (not the tenant register path), public + author=platform.
    assert registry.registrations == []
    reg = registry.platform_registrations[-1]
    assert reg["name"] == "mcp-math"
    assert reg["manifest"]["visibility"] == "public"
    assert reg["manifest"]["author"] == "platform"
    assert reg["manifest"]["base_url"].endswith("/m/mcp-math")
    # The OLD tenant server_name is de-registered (retired) now that Public is live.
    assert "mcp-math-00000000" in registry.retirements
    # The member flow was copied into the platform runtime and the tool row repointed onto it.
    assert len(admin.created) == 1
    repointed = store.tools["tid-add"]
    assert repointed["runtime_id"] == "platform-rt"
    assert repointed["node_red_flow_id"] == admin.created[0]["id"]
    # The member tool is flipped to visibility='public' in the same commit txn so the tools
    # _public_read RLS policy (migration 0007) admits it in a foreign tenant's /m/<slug> resolve.
    assert repointed["visibility"] == "public"


async def test_promote_registry_rejection_does_not_brick(make_client, store, registry) -> None:
    """When the platform registration is rejected, promote fails cleanly and does NOT
    rename/repoint the MCP — it stays private + resolvable, rather than committing the rename first
    and bricking the /m/<old-slug> wire. Any additive platform flow copies are rolled back."""
    mcp = store.add_mcp(slug="mcp-keep-00000000", server_name="mcp-keep-00000000",
                        display_name="Keep", version="1.0.0")
    store.add_tool(tool_id="tid-k", snake_name="keep", node_red_flow_id="f-k", runtime_id="rt1")
    store.links.add((mcp["mcp_id"], "tid-k"))

    async def _reject(*, user_jwt, agent_id, name, manifest, trace_headers=None):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "platform:admin required", status_code=400)

    ac = await _client(make_client, registry)
    admin = ac.app.state.publisher._admin
    registry.register_platform = _reject  # type: ignore[method-assign]

    resp = await ac.post(f"/v1/mcps/{mcp['mcp_id']}/promote")
    assert resp.status_code == 400
    # NOT bricked: the row keeps its private slug/visibility (no destructive rename committed).
    assert store.mcps[mcp["mcp_id"]]["slug"] == "mcp-keep-00000000"
    assert store.mcps[mcp["mcp_id"]]["visibility"] == "private"
    assert store.mcps[mcp["mcp_id"]]["status"] == "active"
    # The tool row is untouched (still on the tenant runtime + still private), and nothing retired.
    assert store.tools["tid-k"]["runtime_id"] == "rt1"
    assert store.tools["tid-k"]["visibility"] == "private"
    assert registry.retirements == []
    # The additive platform flow copy was rolled back (deleted).
    assert admin.deleted == [c["id"] for c in admin.created]


async def test_promote_rehome_failure_rolls_back(make_client, store, registry) -> None:
    """A re-home admin-API failure (copying a member flow into the platform runtime fails) rolls
    back cleanly: the MCP stays private + on its tenant runtime, and the platform registration is
    NEVER attempted (re-home copy precedes the register)."""
    mcp = store.add_mcp(slug="mcp-brk-00000000", server_name="mcp-brk-00000000",
                        display_name="Brk", version="1.0.0")
    store.add_tool(tool_id="tid-b", snake_name="brk", node_red_flow_id="f-b", runtime_id="rt1")
    store.links.add((mcp["mcp_id"], "tid-b"))

    class FailingCreateAdmin(FakeAdmin):
        async def create_flow(self, *, internal_host, admin_token, flow):
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "platform Node-RED unreachable")

    ac = await _client(make_client, registry)
    ac.app.state.publisher._admin = FailingCreateAdmin()

    resp = await ac.post(f"/v1/mcps/{mcp['mcp_id']}/promote")
    assert resp.status_code == 503
    # Private + unrenamed, tool still on the tenant runtime + private, and NO platform registration.
    assert store.mcps[mcp["mcp_id"]]["slug"] == "mcp-brk-00000000"
    assert store.mcps[mcp["mcp_id"]]["visibility"] == "private"
    assert store.tools["tid-b"]["runtime_id"] == "rt1"
    assert store.tools["tid-b"]["visibility"] == "private"
    assert registry.platform_registrations == []
    assert registry.retirements == []


async def test_promote_requires_platform_admin(make_client, store, registry) -> None:
    mcp = store.add_mcp()
    ac = await _client(make_client, registry, scopes=["tool:invoke", "tool:admin", "tenant:admin"])
    resp = await ac.post(f"/v1/mcps/{mcp['mcp_id']}/promote")
    assert resp.status_code == 403


# ── idempotency scope shared across the /w and /m wires (finding #2) ──────────────
async def test_idempotency_shared_across_wires(make_client, store, registry, monkeypatch) -> None:
    async def fake_access(*_a, **_k):
        return "automated"

    invoked = {"n": 0}

    async def fake_invoke(client, **kw):
        invoked["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(mcp_api, "_resolve_tool_access", fake_access)
    monkeypatch.setattr(mcp_api, "invoke_workflow", fake_invoke)

    ac = await _client(make_client, registry)
    created = (await ac.post("/v1/tools", json=_tool_body())).json()
    slug = created["mcp_slug"]
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "adder", "arguments": {"a": 1}}}
    headers = {"Idempotency-Key": "shared-1"}

    first = await ac.post(f"/w/{slug}/mcp", json=call, headers=headers)   # legacy wire
    second = await ac.post(f"/m/{slug}/mcp", json=call, headers=headers)  # aggregating wire
    assert first.json()["result"]["isError"] is False
    assert second.json()["result"]["isError"] is False
    # Same (server_name, capability) scope on both wires -> the second call REPLAYS; flow fired once.
    assert invoked["n"] == 1
