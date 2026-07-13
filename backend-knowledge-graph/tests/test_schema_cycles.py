"""Cyclic / self-referencing DTOs must resolve, not crash.

Bidirectional Pydantic relationships (``User.posts: list[Post]`` + ``Post.author: User``)
are ordinary modelling. They used to trip the engine's cycle detector, because a schema's
nested-field blast edge forced the nested model's FULL assembly, which forced the first
model again. The nested edge now goes through the non-recursive ``schemaDecl`` node, so
existence is answered without recursion — while ``blast_radius`` still reaches schemas
that merely NEST a DTO (the property the recursive read existed for, guarded below).
"""

from __future__ import annotations

from bkg.engine import Engine
from bkg.pipeline import ROOT, apply_sources, install
from bkg.service import GraphService
from bkg.store import open_store

MAIN = (
    "from fastapi import FastAPI\n"
    "from app.schemas import User\n"
    "app = FastAPI()\n"
    "@app.post('/u', response_model=User)\n"
    "def create(u: User): ...\n"
)


def _project(schemas: str) -> dict[str, str]:
    return {"app/__init__.py": "", "app/main.py": MAIN, "app/schemas.py": schemas}


def test_bidirectional_dtos_resolve() -> None:
    src = _project(
        "from __future__ import annotations\n"
        "from pydantic import BaseModel\n"
        "class User(BaseModel):\n    posts: list[Post]\n"
        "class Post(BaseModel):\n    author: User\n"
    )
    svc = GraphService.from_sources(src)
    (ep,) = svc.list_endpoints()  # would previously raise RuntimeError: cycle detected
    assert ep["body"] == "app/schemas.py:User"
    assert ep["response"] == "app/schemas.py:User"
    assert ep["partial"] is False  # both models resolve; nothing is missing
    # editing either side of the cycle blasts the endpoint
    assert svc.blast_radius("app/schemas.py:User") == ["POST:/u"]
    assert svc.blast_radius("app/schemas.py:Post") == ["POST:/u"]


def test_three_way_cycle_resolves() -> None:
    src = _project(
        "from __future__ import annotations\n"
        "from pydantic import BaseModel\n"
        "class User(BaseModel):\n    a: A\n"
        "class A(BaseModel):\n    b: B\n"
        "class B(BaseModel):\n    user: User\n"
    )
    svc = GraphService.from_sources(src)
    (ep,) = svc.list_endpoints()
    assert ep["body"] == "app/schemas.py:User" and ep["partial"] is False
    for model in ("User", "A", "B"):
        assert svc.blast_radius(f"app/schemas.py:{model}") == ["POST:/u"]


def test_self_referencing_dto_resolves() -> None:
    src = _project(
        "from __future__ import annotations\n"
        "from pydantic import BaseModel\n"
        "class User(BaseModel):\n    name: str\n    friends: list[User]\n"
    )
    svc = GraphService.from_sources(src)
    (ep,) = svc.list_endpoints()
    assert ep["body"] == "app/schemas.py:User" and ep["partial"] is False


def test_nested_dto_blast_radius_preserved() -> None:
    """The property the (previously recursive) nested read existed for: editing a DTO that
    is only NESTED inside the body schema must still blast the endpoint."""
    src = _project(
        "from pydantic import BaseModel\n"
        "class Address(BaseModel):\n    street: str\n"
        "class User(BaseModel):\n    home: Address\n"
    )
    svc = GraphService.from_sources(src)
    assert svc.blast_radius("app/schemas.py:Address") == ["POST:/u"]


def test_transitively_nested_dto_blast_radius_preserved() -> None:
    """Nesting is transitive: Outer -> Mid -> Deep. Editing Deep must blast the endpoint
    whose body is Outer. (A non-recursive existence check alone would lose this.)"""
    src = _project(
        "from pydantic import BaseModel\n"
        "class Deep(BaseModel):\n    d: str\n"
        "class Mid(BaseModel):\n    deep: Deep\n"
        "class User(BaseModel):\n    mid: Mid\n"
    )
    svc = GraphService.from_sources(src)
    for model in ("Deep", "Mid", "User"):
        assert svc.blast_radius(f"app/schemas.py:{model}") == ["POST:/u"]


def test_every_model_param_blasts_not_just_the_body() -> None:
    """Only the FIRST model-typed param becomes the body, but every model-typed param is
    still a reference — editing the second one must blast the endpoint too."""
    src = {
        "app/__init__.py": "",
        "app/schemas.py": (
            "from pydantic import BaseModel\n"
            "class ModelA(BaseModel):\n    a: str\n"
            "class ModelB(BaseModel):\n    b: str\n"
        ),
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.schemas import ModelA, ModelB\n"
            "app = FastAPI()\n"
            "@app.post('/x')\n"
            "def x(a: ModelA, b: ModelB): ...\n"
        ),
    }
    svc = GraphService.from_sources(src)
    (ep,) = svc.list_endpoints()
    assert ep["body"] == "app/schemas.py:ModelA"  # first model param is the body
    assert svc.blast_radius("app/schemas.py:ModelB") == ["POST:/x"]


def test_unresolvable_body_dto_marks_endpoint_partial() -> None:
    """The endpoint's own DTO resolution still drives its ``partial`` flag: a body that
    looks like a model but resolves to no project schema is an honest gap."""
    src = {
        "app/__init__.py": "",
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.missing import Ghost\n"
            "app = FastAPI()\n"
            "@app.post('/g')\n"
            "def create(g: Ghost): ...\n"
        ),
    }
    svc = GraphService.from_sources(src)
    (ep,) = svc.list_endpoints()
    assert ep["body"] is None and ep["partial"] is True
    assert ep["confidence"] == "inferred"


def test_mutual_base_inheritance_degrades_instead_of_crashing() -> None:
    """``class A(B)`` + ``class B(A)`` is INVALID Python but it PARSES — which is the
    normal state of a file mid-edit. A static analyzer must never take the whole graph
    down on parseable input: the cycle degrades those schemas to partial."""
    src = _project(
        "from pydantic import BaseModel\n"
        "class User(A):\n    x: int\n"
        "class A(User):\n    y: int\n"
    )
    svc = GraphService.from_sources(src)
    (ep,) = svc.list_endpoints()  # would previously raise RuntimeError: cycle detected
    assert ep["body"] == "app/schemas.py:User"

    # the broken inheritance is reported, not hidden: the schemas caught in the cycle are
    # marked partial (their bases cannot be merged) rather than taking the graph down
    engine = Engine(open_store(":memory:"))
    install(engine)
    apply_sources(engine, src)
    assert engine.query("schemaBaseCycles:all") == ["app/schemas.py:A", "app/schemas.py:User"]
    for model in ("User", "A"):
        schema = engine.query(f"schemaRef:app/schemas.py:{model}")
        assert schema["partial"] is True and schema["confidence"] == "inferred"


def test_self_inheritance_does_not_crash() -> None:
    src = _project("from pydantic import BaseModel\nclass User(User):\n    x: int\n")
    (ep,) = GraphService.from_sources(src).list_endpoints()
    assert ep["body"] == "app/schemas.py:User"


def test_mutually_mounting_routers_degrade_instead_of_crashing() -> None:
    """``a.include_router(b)`` + ``b.include_router(a)`` also parses; a router on a mount
    cycle is treated as unmounted rather than crashing the graph — and the endpoint says
    so (``partial``), because its prefix was truncated and the path is NOT trustworthy."""
    src = {
        "app/__init__.py": "",
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "from app.other import other\n"
            "app = FastAPI()\n"
            "app.include_router(other, prefix='/o')\n"
            "@app.get('/root')\n"
            "def root(): ...\n"
        ),
        "app/other.py": (
            "from fastapi import APIRouter\n"
            "from app.main import app\n"
            "other = APIRouter()\n"
            "other.include_router(app, prefix='/back')\n"
            "@other.get('/x')\n"
            "def x(): ...\n"
        ),
    }
    eps = GraphService.from_sources(src).list_endpoints()
    assert {ep["resolved_path"] for ep in eps} == {"/root", "/x"}  # both routes still served
    # honest degradation: never serve a truncated path as static-certain
    for ep in eps:
        assert ep["partial"] is True
        assert ep["confidence"] == "inferred"


def test_duplicate_class_name_does_not_defeat_the_base_cycle_guard() -> None:
    """A redeclared class (copy-paste / mid-edit) parses fine. The cycle guard must resolve
    the SAME declaration schema_ref does — the first — or it analyzes a graph that isn't
    the one being walked, and the crash comes back."""
    src = _project(
        "from pydantic import BaseModel\n"
        "class User(Base):\n    x: int\n"
        "class Base(User):\n    y: int\n"
        "class User(BaseModel):\n    z: int\n"  # duplicate: LAST decl has no project base
    )
    svc = GraphService.from_sources(src)
    (ep,) = svc.list_endpoints()  # must not raise RuntimeError: cycle detected
    assert ep["body"] == "app/schemas.py:User"

    engine = Engine(open_store(":memory:"))
    install(engine)
    apply_sources(engine, src)
    assert engine.query("schemaBaseCycles:all") == ["app/schemas.py:Base", "app/schemas.py:User"]


def test_duplicate_class_name_does_not_hide_nested_blast() -> None:
    """Same first/last mismatch, but silent: schemaDeps must use the FIRST declaration, or
    blast_radius returns [] for a DTO that is genuinely nested."""
    src = _project(
        "from pydantic import BaseModel\n"
        "class Address(BaseModel):\n    street: str\n"
        "class User(BaseModel):\n    home: Address\n"  # first User NESTS Address
        "class User(BaseModel):\n    z: int\n"  # duplicate: last decl nests nothing
    )
    svc = GraphService.from_sources(src)
    # both declarations are listed (the raw decl list is honest about the redeclaration),
    # but everything that RESOLVES a model must agree on the first one
    users = [s for s in svc.list_schemas() if s["name"] == "User"]
    assert len(users) == 2
    assert [f["name"] for f in users[0]["fields"]] == ["home"]  # first declaration nests Address
    assert svc.blast_radius("app/schemas.py:Address") == ["POST:/u"]


def test_blast_radius_excludes_deleted_endpoints() -> None:
    """On a WARM engine the store still holds reverse-dep rows for routes that were since
    deleted; blast_radius must answer from the live graph, not those stale rows."""
    schemas = "from pydantic import BaseModel\nclass User(BaseModel):\n    x: int\n"
    two = (
        "from fastapi import FastAPI\n"
        "from app.schemas import User\n"
        "app = FastAPI()\n"
        "@app.post('/a', response_model=User)\n"
        "def a(u: User): ...\n"
        "@app.post('/b', response_model=User)\n"
        "def b(u: User): ...\n"
    )
    svc = GraphService.from_sources(
        {"app/__init__.py": "", "app/main.py": two, "app/schemas.py": schemas}
    )
    assert svc.blast_radius("app/schemas.py:User") == [
        "POST:/a",
        "POST:/b",
    ]
    # delete route /b on the same (warm) engine
    svc.update_file("app/main.py", two[: two.index("@app.post('/b'")])
    assert [ep["resolved_path"] for ep in svc.list_endpoints()] == ["/a"]
    assert svc.blast_radius("app/schemas.py:User") == ["POST:/a"]


def test_cyclic_project_is_incrementally_deterministic() -> None:
    """The moat still holds on a cyclic graph: incremental == rebuild, zero cascade."""
    src = _project(
        "from __future__ import annotations\n"
        "from pydantic import BaseModel\n"
        "class User(BaseModel):\n    posts: list[Post]\n"
        "class Post(BaseModel):\n    author: User\n"
    )

    def build(sources: dict[str, str]) -> Engine:
        engine = Engine(open_store(":memory:"))
        install(engine)
        apply_sources(engine, sources)
        return engine

    engine = build(src)
    engine.snapshot_digest(ROOT)
    edited = dict(src)
    edited["app/schemas.py"] = src["app/schemas.py"].replace("author: User", "author: User\n    n: int")
    engine.set_input("fileText:app/schemas.py", edited["app/schemas.py"])

    assert engine.snapshot_digest(ROOT) == build(edited).snapshot_digest(ROOT)
    assert engine.dep_map(ROOT) == build(edited).dep_map(ROOT)
    engine.reset_counters()
    engine.snapshot_digest(ROOT)
    assert engine.recompute_count == 0  # idempotent re-query
