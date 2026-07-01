"""KB ACL deny (403 FORBIDDEN_KB) + RLS tenant-isolation tests."""

from __future__ import annotations

import pytest

from .conftest import OTHER_TENANT, TEST_TENANT, make_principal

AUTH = {"Authorization": "Bearer test"}


@pytest.mark.asyncio
async def test_private_kb_query_denied_for_non_creator_403(app_client, auth_as) -> None:  # noqa: ANN001
    # private=True -> no tenant-wide ACL; only the creator (+ explicit adds) can access.
    kb = (await app_client.post(
        "/v1/kbs", json={"name": "secret", "private": True}, headers=AUTH
    )).json()
    # A DIFFERENT agent in the same tenant is denied (no matching ACL row).
    auth_as(make_principal(agent_id="00000000-0000-0000-0000-0000000000ee"))
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query", json={"query": "x"}, headers=AUTH
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN_KB"


@pytest.mark.asyncio
async def test_private_kb_creator_allowed(app_client, auth_as) -> None:  # noqa: ANN001
    # The creator gets a full-access ACL on a private KB and can query it.
    kb = (await app_client.post(
        "/v1/kbs", json={"name": "mine", "private": True}, headers=AUTH
    )).json()
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query", json={"query": "x", "min_score": 0.0}, headers=AUTH
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_acl_grant_then_query_allowed(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = (await app_client.post(
        "/v1/kbs", json={"name": "granted", "private": True}, headers=AUTH
    )).json()
    # Add an explicit agent ACL for the test agent.
    add = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/acls",
        json={
            "principal_type": "agent",
            "principal_id": "00000000-0000-0000-0000-0000000000bb",
            "permissions": ["query"],
        },
        headers=AUTH,
    )
    assert add.status_code == 201
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query", json={"query": "x", "min_score": 0.0}, headers=AUTH
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_scoped_acl_partitions_by_end_user(app_client, auth_as) -> None:  # noqa: ANN001
    # External SaaS pattern: a user-scoped ACL admits only the matching cypherx:user_id.
    kb = (await app_client.post(
        "/v1/kbs", json={"name": "peruser", "private": True}, headers=AUTH
    )).json()
    await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/acls",
        json={"principal_type": "user", "principal_id": "user-123", "permissions": ["query"]},
        headers=AUTH,
    )
    # A pure user-principal (no agent_id) carrying cypherx:user_id=user-123 is allowed.
    auth_as(make_principal(agent_id=None, user_id="user-123", principal_type="user"))
    ok = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query", json={"query": "x", "min_score": 0.0}, headers=AUTH
    )
    assert ok.status_code == 200
    # user-999 is denied (no matching ACL row).
    auth_as(make_principal(agent_id=None, user_id="user-999", principal_type="user"))
    denied = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query", json={"query": "x"}, headers=AUTH
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_rls_tenant_isolation_set_local(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    """Every tenant-scoped op sets app.tenant_id; a second tenant cannot see the first's KB."""
    # Tenant A creates a KB.
    auth_as(make_principal(tenant_id=TEST_TENANT))
    kb = (await app_client.post("/v1/kbs", json={"name": "tenantA-kb"}, headers=AUTH)).json()
    assert TEST_TENANT in fake_db.tenant_contexts  # SET LOCAL app.tenant_id was issued

    # Tenant B cannot GET tenant A's KB (RLS filters by app.tenant_id).
    auth_as(make_principal(tenant_id=OTHER_TENANT, agent_id="00000000-0000-0000-0000-0000000000dd"))
    resp = await app_client.get(f"/v1/kbs/{kb['kb_id']}", headers=AUTH)
    assert resp.status_code == 404
    assert OTHER_TENANT in fake_db.tenant_contexts

    # Tenant B's KB list is empty (does not leak tenant A's row).
    listed = await app_client.get("/v1/kbs", headers=AUTH)
    assert listed.json() == []


@pytest.mark.asyncio
async def test_rls_isolation_on_query(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    auth_as(make_principal(tenant_id=TEST_TENANT))
    kb = (await app_client.post("/v1/kbs", json={"name": "isoq"}, headers=AUTH)).json()
    await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents",
        json={"name": "d.md", "content": "alpha beta gamma", "source_type": "markdown"},
        headers=AUTH,
    )
    # Tenant B querying the same kb_id -> 404 (KB not visible under B's RLS context).
    auth_as(make_principal(tenant_id=OTHER_TENANT, agent_id="00000000-0000-0000-0000-0000000000dd"))
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query", json={"query": "alpha"}, headers=AUTH
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_acl_admin_required_for_acl_management(app_client, auth_as) -> None:  # noqa: ANN001
    kb = (await app_client.post("/v1/kbs", json={"name": "adminkb"}, headers=AUTH)).json()
    # A principal without rag:admin scope cannot manage ACLs (403 FORBIDDEN, scope check).
    auth_as(make_principal(scopes=["rag:query"]))
    resp = await app_client.get(f"/v1/kbs/{kb['kb_id']}/acls", headers=AUTH)
    assert resp.status_code == 403
