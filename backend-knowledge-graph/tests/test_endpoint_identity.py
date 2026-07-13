"""Endpoint identity = the fully RESOLVED route, one node per CONCRETE EXPOSURE.

A router mounted twice is genuinely exposed twice. The graph must hold both exposures as
independent Endpoint nodes (distinct ids, distinct resolved paths, distinct middleware /
tag chains), each pointing back at the ONE shared route declaration (``route``) and the
ONE shared implementation (``handler_id``) — so the graph preserves the implementation
and every concrete exposure of it.
"""

from __future__ import annotations

from bkg.engine import Engine
from bkg.pipeline import ROOT, apply_sources, install
from bkg.service import GraphService
from bkg.store import open_store

USERS = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "@router.get('/u')\n"
    "def get_user(): ...\n"
)


def _sources(main: str) -> dict[str, str]:
    return {
        "app/__init__.py": "",
        "app/routers/__init__.py": "",
        "app/routers/users.py": USERS,
        "app/main.py": main,
    }


def test_router_mounted_twice_yields_two_independent_endpoints() -> None:
    src = _sources(
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/v1')\n"
        "app.include_router(users.router, prefix='/v2')\n"
    )
    endpoints = GraphService.from_sources(src).list_endpoints()

    assert [ep["id"] for ep in endpoints] == ["GET:/v1/u", "GET:/v2/u"]
    assert {ep["resolved_path"] for ep in endpoints} == {"/v1/u", "/v2/u"}

    # both exposures share ONE declaration and ONE implementation
    assert {ep["route"] for ep in endpoints} == {"app/routers/users.py:router:GET:/u"}
    assert {ep["handler_id"] for ep in endpoints} == {"app/routers/users.py#get_user"}
    assert {ep["handler"] for ep in endpoints} == {"get_user"}


def test_mount_chains_compose_across_nesting() -> None:
    """A router mounted twice under a parent mounted twice is exposed 2x2 = 4 times."""
    src = {
        "app/__init__.py": "",
        "app/inner.py": (
            "from fastapi import APIRouter\n"
            "inner = APIRouter()\n"
            "@inner.get('/i')\n"
            "def i(): ...\n"
        ),
        "app/mid.py": (
            "from fastapi import APIRouter\n"
            "from app.inner import inner\n"
            "mid = APIRouter()\n"
            "mid.include_router(inner, prefix='/a')\n"
            "mid.include_router(inner, prefix='/b')\n"
        ),
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.mid import mid\n"
            "app = FastAPI()\n"
            "app.include_router(mid, prefix='/x')\n"
            "app.include_router(mid, prefix='/y')\n"
        ),
    }
    ids = [ep["id"] for ep in GraphService.from_sources(src).list_endpoints()]
    assert ids == ["GET:/x/a/i", "GET:/x/b/i", "GET:/y/a/i", "GET:/y/b/i"]


def test_each_exposure_carries_its_own_middleware_and_tags() -> None:
    src = _sources(
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/v1', tags=['v1'])\n"
        "app.include_router(users.router, prefix='/v2', tags=['v2'])\n"
    )
    by_id = {ep["id"]: ep for ep in GraphService.from_sources(src).list_endpoints()}
    assert by_id["GET:/v1/u"]["tags"] == ["v1"]
    assert by_id["GET:/v2/u"]["tags"] == ["v2"]  # exposures do NOT bleed into each other


def test_two_mounts_at_the_same_prefix_are_one_exposure() -> None:
    """Mounting the same router twice at the SAME prefix is one concrete path, not two."""
    src = _sources(
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/v1')\n"
        "app.include_router(users.router, prefix='/v1')\n"
    )
    assert [ep["id"] for ep in GraphService.from_sources(src).list_endpoints()] == ["GET:/v1/u"]


def test_served_ids_are_unique_even_on_a_route_collision() -> None:
    """Two declarations resolving to the same path is a real routing collision; the first
    wins (as the framework's first-match-wins router does) so ids stay unique."""
    src = {
        "app/__init__.py": "",
        "app/main.py": (
            "from fastapi import FastAPI, APIRouter\n"
            "app = FastAPI()\n"
            "other = APIRouter()\n"
            "@other.get('/u')\n"
            "def other_handler(): ...\n"
            "@app.get('/u')\n"
            "def app_handler(): ...\n"
            "app.include_router(other)\n"
        ),
    }
    endpoints = GraphService.from_sources(src).list_endpoints()
    assert len({ep["id"] for ep in endpoints}) == len(endpoints)  # ids unique
    assert [ep["id"] for ep in endpoints] == ["GET:/u"]


def test_blast_radius_answers_in_resolved_ids_for_every_exposure() -> None:
    src = {
        "app/__init__.py": "",
        "app/schemas.py": "from pydantic import BaseModel\nclass User(BaseModel):\n    x: int\n",
        "app/routers/__init__.py": "",
        "app/routers/users.py": (
            "from fastapi import APIRouter\n"
            "from app.schemas import User\n"
            "router = APIRouter()\n"
            "@router.post('/u', response_model=User)\n"
            "def create(u: User): ...\n"
        ),
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.routers import users\n"
            "app = FastAPI()\n"
            "app.include_router(users.router, prefix='/v1')\n"
            "app.include_router(users.router, prefix='/v2')\n"
        ),
    }
    svc = GraphService.from_sources(src)
    # editing the DTO blasts BOTH concrete exposures of the shared handler
    assert svc.blast_radius("app/schemas.py:User") == ["POST:/v1/u", "POST:/v2/u"]
    assert svc.get_endpoint_by_id("POST:/v2/u")["resolved_path"] == "/v2/u"
    assert svc.get_endpoint("POST", "/v1/u")["route"] == "app/routers/users.py:router:POST:/u"


def test_adding_a_second_mount_is_incremental_and_deterministic() -> None:
    """The moat holds across the fan-out: adding a mount adds an exposure, incrementally."""
    one = (
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/v1')\n"
    )
    two = one + "app.include_router(users.router, prefix='/v2')\n"

    def build(main: str) -> Engine:
        engine = Engine(open_store(":memory:"))
        install(engine)
        apply_sources(engine, _sources(main))
        return engine

    engine = build(one)
    assert len(engine.query(ROOT)) == 1
    engine.set_input("fileText:app/main.py", two)
    assert len(engine.query(ROOT)) == 2  # the new exposure appears

    fresh = build(two)
    assert engine.snapshot_digest(ROOT) == fresh.snapshot_digest(ROOT)
    assert engine.dep_map(ROOT) == fresh.dep_map(ROOT)
    engine.reset_counters()
    engine.snapshot_digest(ROOT)
    assert engine.recompute_count == 0  # idempotent
