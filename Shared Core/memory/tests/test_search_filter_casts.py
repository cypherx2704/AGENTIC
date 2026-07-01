"""Regression: POST /v1/memories/search must return 200 with no filters AND with each
optional filter — and the PG repository's optional/nullable bound params MUST carry an
explicit type cast.

Background (the bug this guards): on live Postgres every search 500'd with psycopg
``AmbiguousParameter`` ("could not determine data type of parameter"). The PASS-2 search
query bound the optional filter predicates as untyped params, e.g.

    (%(type)s IS NULL OR m.type = %(type)s)

Postgres cannot infer the type of a bare param that only ever appears against ``IS NULL``
at PREPARE time, so prepare failed and search was 100% dead. The fix adds an explicit cast
to every such param (``::text``, ``::text[]``, ``::boolean``, ``::uuid[]``) so the type is
unambiguous.

IMPORTANT — why the SQL-source assertion exists: the app-level tests below run against the
in-memory ``InMemoryRepository`` (conftest NULLs ``db_pool`` and swaps the repo), which
executes NO SQL and therefore CANNOT reproduce the psycopg ``AmbiguousParameter`` failure.
The API-path tests only prove the request/response contract still returns 200. To actually
pin the live-PG cast regression without a running Postgres, ``test_pg_search_params_are_cast``
statically asserts the cast appears in the PG query text. If you ever need full coverage,
run the suite against a live pgvector instance (the in-memory fake will not catch a missing
cast).
"""

from __future__ import annotations

import inspect

import pytest

from _helpers import bind_principal, make_principal
from memory_service.services import pg_repository


@pytest.mark.asyncio
async def test_search_no_filters_returns_200(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    await ac.post("/v1/memories", json={"content": "alpha bravo charlie"})

    s = await ac.post("/v1/memories/search", json={"query": "alpha", "top_k": 5})
    assert s.status_code == 200, s.text


@pytest.mark.asyncio
async def test_search_with_each_filter_returns_200(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    await ac.post(
        "/v1/memories",
        json={
            "content": "scoped fact",
            "type": "fact",
            "tags": ["t1", "t2"],
            "session_scope_id": "sess-1",
            "agent_scope_id": "agent-scope-1",
        },
    )

    # Each optional filter exercised independently — every one must return 200, not 500.
    for body in (
        {"query": "scoped", "type": "fact"},
        {"query": "scoped", "tags": ["t1"]},
        {"query": "scoped", "session_scope_id": "sess-1"},
        {"query": "scoped", "agent_scope_id": "agent-scope-1"},
        {"query": "scoped", "include_shared": False},
        {"query": "scoped", "include_superseded": True},
        {"query": "scoped", "include_superseded": False},
        # All filters at once.
        {
            "query": "scoped",
            "type": "fact",
            "tags": ["t1", "t2"],
            "session_scope_id": "sess-1",
            "agent_scope_id": "agent-scope-1",
            "include_shared": True,
            "include_superseded": False,
        },
    ):
        s = await ac.post("/v1/memories/search", json=body)
        assert s.status_code == 200, f"{body} -> {s.status_code} {s.text}"


def test_pg_search_params_are_cast() -> None:
    """The optional/nullable bound params in the PG search + by-id queries MUST be cast.

    This is the live-PG regression: without these casts psycopg raises AmbiguousParameter at
    prepare time. The in-memory test repo cannot reproduce that (it runs no SQL), so we
    assert on the query SQL text directly.
    """
    search_src = inspect.getsource(pg_repository.PgMemoryRepository.search)
    by_id_src = inspect.getsource(pg_repository.PgMemoryRepository.get_by_id)

    # Every optional/nullable predicate param in PASS-2 of search must carry an explicit cast.
    required_search_casts = [
        "%(type)s::text IS NULL",
        "m.type = %(type)s::text",
        "%(tags)s::text[] IS NULL",
        "m.tags @> %(tags)s::text[]",
        "%(session_scope)s::text IS NULL",
        "m.session_scope_id = %(session_scope)s::text",
        "%(agent_scope)s::text IS NULL",
        "m.agent_scope_id = %(agent_scope)s::text",
        "NOT %(current_only)s::boolean",
        "%(include_shared)s::boolean",
        "%(visibility)s::text = 'tenant'",
    ]
    for needle in required_search_casts:
        assert needle in search_src, f"search query is missing required cast: {needle!r}"

    # The inline last_accessed bump compares a Python str list to a uuid column via ANY().
    assert "ANY(%s::uuid[])" in search_src, "inline last_accessed bump must cast ids to uuid[]"

    # get_by_id mirrors the visibility predicate — keep its params cast too.
    for needle in ("%(ctype)s::text", "%(cid)s::text", "%(visibility)s::text = 'tenant'"):
        assert needle in by_id_src, f"get_by_id query is missing required cast: {needle!r}"


def test_pg_search_has_no_uncast_isnull_param() -> None:
    """No ``%(name)s IS NULL`` (uncast) remains in the search query — that is the exact
    untyped-NULL pattern that triggered AmbiguousParameter."""
    import re

    search_src = inspect.getsource(pg_repository.PgMemoryRepository.search)
    # Match `%(foo)s IS NULL` NOT immediately preceded by a `::cast`.
    uncast = re.findall(r"%\((\w+)\)s\s+IS NULL", search_src)
    cast = re.findall(r"%\((\w+)\)s::\w[\w\[\]]*\s+IS NULL", search_src)
    offenders = [name for name in uncast if name not in cast]
    assert not offenders, f"uncast `%(name)s IS NULL` params remain: {offenders}"
