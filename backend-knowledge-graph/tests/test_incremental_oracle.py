"""The determinism oracle — the P1 moat proof.

For random edit sequences AND targeted scenarios, the incrementally-maintained
graph must be:
  (1) byte-identical to a from-scratch rebuild at the same state (digest),
  (2) dependency-edge-identical to a fresh rebuild (no stale/extra/missing deps
      hiding behind a coincidentally-correct value), and
  (3) genuinely incremental — a redundant re-query recomputes NOTHING, a no-op
      edit advances zero downstream facts, and a single-route edit does not
      cascade to sibling endpoints.

(1) is the classic determinism gate; (2) is the "no latent stale deps" gate that
a value digest cannot provide; (3) is what proves it stays type-(III) incremental
rather than silently degrading to re-parse-everything.
"""

from __future__ import annotations

import random

import synthetic as syn
from bkg.engine import Engine
from bkg.store import open_store

ROOT = syn.ROOT
EXTRA = "app/routers/extra.py"


def _incremental_engine(world: syn.World) -> Engine:
    engine = Engine(open_store(":memory:"))
    syn.install(engine)
    syn.apply_full(engine, world)
    return engine


def _fresh(world: syn.World) -> tuple[str, dict[str, list[str]]]:
    engine = syn.build_fresh(open_store(":memory:"), world)
    return engine.snapshot_digest(ROOT), engine.dep_map(ROOT)


def _apply_random_edit(rng: random.Random, engine: Engine, world: syn.World) -> None:
    users = "app/routers/users.py"
    main = "app/main.py"
    ops = ["comment", "add", "mountprefix"]
    if world.files[users]["routes"]:
        ops += ["line", "del", "reorder"]
    ops.append("rmfile" if EXTRA in world.files else "addfile")
    op = rng.choice(ops)
    if op == "comment":
        syn.edit_comment(engine, world, rng.choice(world.files_all()))
    elif op == "add":
        syn.edit_add_route(engine, world, users)
    elif op == "mountprefix":
        syn.edit_mount_prefix(engine, world, main, 0, rng.choice(["/api/users", "/v2/users", "/u", ""]))
    elif op == "line":
        syn.edit_route_line(engine, world, users, rng.randrange(len(world.files[users]["routes"])))
    elif op == "del":
        syn.edit_del_route(engine, world, users, rng.randrange(len(world.files[users]["routes"])))
    elif op == "reorder":
        syn.edit_reorder_routes(engine, world, users)
    elif op == "addfile":
        content = {
            "raw_version": 0,
            "routes": [{"router": "r", "method": "GET", "path": "/x", "handler": "hx", "line": 1}],
            "mounts": [],
            "middleware": [],
        }
        syn.edit_add_file(engine, world, EXTRA, content)
    elif op == "rmfile":
        syn.edit_remove_file(engine, world, EXTRA)


def test_oracle_random_edit_sequences() -> None:
    for seed in range(20):
        rng = random.Random(seed)
        world = syn.seed_world()
        engine = _incremental_engine(world)
        engine.snapshot_digest(ROOT)  # prime
        for step in range(20):
            _apply_random_edit(rng, engine, world)

            inc_digest = engine.snapshot_digest(ROOT)
            inc_deps = engine.dep_map(ROOT)

            # (3) idempotence: a redundant re-query at the same revision must
            # recompute NOTHING (kills a "recompute-everything" mutant).
            engine.reset_counters()
            engine.snapshot_digest(ROOT)
            assert engine.recompute_count == 0, (
                f"seed={seed} step={step}: redundant re-query recomputed {engine.recomputed}"
            )

            fresh_digest, fresh_deps = _fresh(world)
            assert inc_digest == fresh_digest, f"seed={seed} step={step}: snapshot digest mismatch"
            assert inc_deps == fresh_deps, f"seed={seed} step={step}: dependency-edge mismatch"


def _changed_at_current_rev(engine: Engine, root: str) -> list[str]:
    rows = engine.snapshot_rows(root)
    rev = engine._store.get_revision()
    return [
        r.key
        for r in rows
        if r.changed_rev == rev and not r.key.startswith("file:") and r.key != "files:all"
    ]


def test_comment_edit_zero_downstream_cascade() -> None:
    world = syn.seed_world()
    engine = _incremental_engine(world)
    engine.snapshot_digest(ROOT)  # settle

    engine.reset_counters()
    syn.edit_comment(engine, world, "app/routers/users.py")
    engine.snapshot_digest(ROOT)

    # Only the edited file's own projections may recompute (and they backdate).
    allowed = {"routeDeclList:app/routers/users.py", "mountDeclList:app/routers/users.py"}
    assert engine.recomputed <= allowed, f"unexpected recomputes: {engine.recomputed - allowed}"
    assert not any(k.startswith("endpoint:") for k in engine.recomputed)
    # No non-input fact advanced its changed_rev — a true no-op downstream.
    assert _changed_at_current_rev(engine, ROOT) == []


def test_single_route_edit_does_not_touch_siblings() -> None:
    world = syn.seed_world()
    engine = _incremental_engine(world)
    engine.snapshot_digest(ROOT)

    users = "app/routers/users.py"
    get_ep = "endpoint:app/routers/users.py:router:GET:/{user_id}"
    post_ep = "endpoint:app/routers/users.py:router:POST:/"

    # routes are sorted (GET before POST); index 0 == the GET route.
    syn.edit_route_line(engine, world, users, 0)
    engine.snapshot_digest(ROOT)
    changed = set(_changed_at_current_rev(engine, ROOT))

    assert get_ep in changed  # the edited route's endpoint changed...
    assert post_ep not in changed  # ...its sibling did NOT (firewall held)

    assert engine.snapshot_digest(ROOT) == _fresh(world)[0]


def test_mount_prefix_change_updates_all_endpoints_under_it() -> None:
    world = syn.seed_world()
    engine = _incremental_engine(world)
    engine.snapshot_digest(ROOT)

    syn.edit_mount_prefix(engine, world, "app/main.py", 0, "/v2/users")
    engine.snapshot_digest(ROOT)
    changed = set(_changed_at_current_rev(engine, ROOT))

    assert "endpoint:app/routers/users.py:router:GET:/{user_id}" in changed
    assert "endpoint:app/routers/users.py:router:POST:/" in changed

    fresh_digest, fresh_deps = _fresh(world)
    assert engine.snapshot_digest(ROOT) == fresh_digest
    assert engine.dep_map(ROOT) == fresh_deps


def test_add_then_delete_route_restores_digest() -> None:
    world = syn.seed_world()
    engine = _incremental_engine(world)
    base = engine.snapshot_digest(ROOT)

    syn.edit_add_route(engine, world, "app/routers/users.py")
    after_add = engine.snapshot_digest(ROOT)
    assert after_add != base
    assert after_add == _fresh(world)[0]

    users = "app/routers/users.py"
    syn.edit_del_route(engine, world, users, len(world.files[users]["routes"]) - 1)
    after_del = engine.snapshot_digest(ROOT)
    assert after_del == base  # reachability GC: the deleted route's facts are gone
    assert after_del == _fresh(world)[0]


def test_add_then_delete_file_restores_digest() -> None:
    """C1: file removal must invalidate the reverse-dep closure — not crash or
    serve stale — and return to the exact prior digest."""
    world = syn.seed_world()
    engine = _incremental_engine(world)
    base = engine.snapshot_digest(ROOT)

    content = {
        "raw_version": 0,
        "routes": [{"router": "r", "method": "GET", "path": "/ping", "handler": "ping", "line": 1}],
        "mounts": [],
        "middleware": [],
    }
    syn.edit_add_file(engine, world, EXTRA, content)
    after_add = engine.snapshot_digest(ROOT)
    assert after_add != base
    assert after_add == _fresh(world)[0]
    assert engine.dep_map(ROOT) == _fresh(world)[1]

    syn.edit_remove_file(engine, world, EXTRA)
    after_del = engine.snapshot_digest(ROOT)
    assert after_del == base
    fresh_digest, fresh_deps = _fresh(world)
    assert after_del == fresh_digest
    assert engine.dep_map(ROOT) == fresh_deps


def _deep_world() -> syn.World:
    """A depth-2 mount chain: main -> api (/api) -> users (/users)."""
    w = syn.World()
    w.files["app/main.py"] = {
        "raw_version": 0,
        "routes": [],
        "mounts": [
            {
                "router_local": "app",
                "prefix": "/api",
                "target": "app/routers/api.py:api_router",
                "middleware": ["Outer"],
            }
        ],
        "middleware": [],
    }
    w.files["app/routers/api.py"] = {
        "raw_version": 0,
        "routes": [],
        "mounts": [
            {
                "router_local": "api_router",
                "prefix": "/users",
                "target": "app/routers/users.py:router",
                "middleware": ["Inner"],
            }
        ],
        "middleware": [],
    }
    w.files["app/routers/users.py"] = {
        "raw_version": 0,
        "routes": [{"router": "router", "method": "GET", "path": "/{id}", "handler": "get_user", "line": 3}],
        "mounts": [],
        "middleware": [],
    }
    return w


def test_deep_mount_chain_resolves_and_stays_incremental() -> None:
    world = _deep_world()
    engine = _incremental_engine(world)

    ep = engine.query("endpoint:app/routers/users.py:router:GET:/{id}")
    assert ep["resolved_path"] == "/api/users/{id}"  # recursive prefix join
    assert ep["middleware_chain"] == ["Outer", "Inner"]  # root-to-leaf order

    # edit the OUTER prefix -> the deep endpoint must update, and match a rebuild
    syn.edit_mount_prefix(engine, world, "app/main.py", 0, "/v2")
    ep2 = engine.query("endpoint:app/routers/users.py:router:GET:/{id}")
    assert ep2["resolved_path"] == "/v2/users/{id}"
    assert engine.snapshot_digest(ROOT) == _fresh(world)[0]


def test_reload_engine_reproduces_digest(tmp_path) -> None:
    """A fresh Engine over an existing on-disk store reproduces the same snapshot
    (inputs are identified structurally, so nothing volatile needs rehydrating)."""
    db = str(tmp_path / "graph.db")
    world = syn.seed_world()

    engine = Engine(open_store(db))
    syn.install(engine)
    syn.apply_full(engine, world)
    before = engine.snapshot_digest(ROOT)
    engine._store.close()

    reopened = Engine(open_store(db))
    syn.install(reopened)
    after = reopened.snapshot_digest(ROOT)
    reopened._store.close()

    assert before == after
