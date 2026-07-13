"""Mount resolution + endpoint identity.

Two bugs this pins:
- a router RE-EXPORTED through a package ``__init__`` used to resolve to the ``__init__``
  file, match no mount, and silently serve its routes with the mount PREFIX DROPPED — as
  ``static-certain``. Wrong data at full confidence is the worst failure mode there is.
- two handlers on the same ``router+method+path`` collide on the route id; everything
  downstream of ``graph:all`` assumes ids are unique.
"""

from __future__ import annotations

from bkg.service import GraphService

USERS = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "@router.get('/u')\n"
    "def u(): ...\n"
)


def test_router_reexported_via_package_init_keeps_its_prefix() -> None:
    src = {
        "app/__init__.py": "",
        "app/routers/__init__.py": "from app.routers.users import router\n",  # re-export
        "app/routers/users.py": USERS,
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.routers import router\n"  # imported from the PACKAGE, not the module
            "app = FastAPI()\n"
            "app.include_router(router, prefix='/v1')\n"
        ),
    }
    (ep,) = GraphService.from_sources(src).list_endpoints()
    assert ep["resolved_path"] == "/v1/u"  # was "/u" — the prefix was silently dropped
    assert ep["partial"] is False


def test_router_reexported_through_two_packages() -> None:
    """The chase is transitive: api/__init__ re-exports from routers/__init__, which
    re-exports from users.py."""
    src = {
        "app/__init__.py": "",
        "app/api/__init__.py": "from app.routers import router\n",
        "app/routers/__init__.py": "from app.routers.users import router\n",
        "app/routers/users.py": USERS,
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.api import router\n"
            "app = FastAPI()\n"
            "app.include_router(router, prefix='/v1')\n"
        ),
    }
    (ep,) = GraphService.from_sources(src).list_endpoints()
    assert ep["resolved_path"] == "/v1/u"


def test_direct_module_import_still_resolves() -> None:
    """The ordinary (non-re-exported) path must be unaffected by the chase."""
    src = {
        "app/__init__.py": "",
        "app/routers/__init__.py": "",
        "app/routers/users.py": USERS,
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.routers import users\n"
            "app = FastAPI()\n"
            "app.include_router(users.router, prefix='/v1')\n"
        ),
    }
    (ep,) = GraphService.from_sources(src).list_endpoints()
    assert ep["resolved_path"] == "/v1/u"


def test_circular_reexport_does_not_hang_or_crash() -> None:
    """Two modules re-exporting each other's name parses fine; the chase is bounded."""
    src = {
        "app/__init__.py": "",
        "app/a.py": "from app.b import router\n",
        "app/b.py": "from app.a import router\n",
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.a import router\n"
            "app = FastAPI()\n"
            "app.include_router(router, prefix='/v1')\n"
        ),
    }
    assert GraphService.from_sources(src).list_endpoints() == []  # no routes, but no hang


def test_duplicate_route_id_yields_one_endpoint() -> None:
    """Two handlers on the same router+method+path collide on the id. Keep the first (the
    framework's own first-match-wins semantics make the second unreachable anyway) so the
    id-uniqueness everything downstream of graph:all assumes actually holds."""
    src = {
        "app/__init__.py": "",
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/x')\n"
            "def first(): ...\n"
            "@app.get('/x')\n"
            "def second(): ...\n"
        ),
    }
    svc = GraphService.from_sources(src)
    eps = svc.list_endpoints()
    assert len(eps) == 1  # was 2 — the same id twice
    assert eps[0]["handler"] == "first"  # first registered route wins, as the framework does
    assert len({ep["id"] for ep in eps}) == len(eps)  # ids are unique
    assert svc.trust_summary()["endpoints"] == 1  # was double-counted
