"""Phase-6 cross-service e2e harness bootstrap.

This directory hosts an in-process END-TO-END test that threads ONE shared artifact — the
Contract-4 MCP manifest — through all three services (tool-flow-bridge -> tool-registry ->
xAgent) driven via their ASGI apps / real stage code, with only the DBs and Node-RED faked.

The three services live in separate source trees and are NOT installed into one venv, so this
conftest makes them importable side-by-side (each is its own top-level package —
``tool_flow_bridge`` / ``tool_registry`` / ``agent_runtime`` — so there is no collision) and
pins a network-free, DB-free environment BEFORE any service ``Settings`` is instantiated
(each service caches Settings via an lru_cache; the first read wins for the process).

Run it (from the repo root) with a venv that has every service's deps — the xAgent venv is a
superset:

    xAgent/ax-1/.venv/Scripts/python.exe -m pytest tests/e2e -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Make all three services importable (their src/ trees on sys.path) ────────────────
_ROOT = Path(__file__).resolve().parents[2]
for _rel in (
    "Tools/tool-flow-bridge/src",
    "Tools/tool-registry/src",
    "xAgent/ax-1/src",
):
    _p = str(_ROOT / _rel)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Pin a harmless, network-free environment (all three read these by field name) ────
# None of the services open a real DB in this harness: flow-bridge's db access is
# monkeypatched to a FakeStore, the registry's app.state.db_pool is a scripted double,
# and xAgent is exercised at the stage level (no app / no pool).
os.environ.setdefault("ENVIRONMENT", "test")
# Point the (fail-soft, never-queried) service pools at a closed port so a stray background
# open() is refused instantly and NO real Postgres is ever contacted. All real DB access in
# this harness goes through the FakeStore / scripted registry double instead.
os.environ.setdefault("DATABASE_URL", "postgresql://e2e:e2e@127.0.0.1:59999/none")
os.environ.setdefault("PROVISIONER_MODE", "static")            # flow-bridge: single static Node-RED
os.environ.setdefault("SEED_PLATFORM_TOOLS", "false")          # registry: no seed in the lifespan
os.environ.setdefault("SERVICE_BOOTSTRAP_SECRET", "e2e-bootstrap-secret")  # xAgent: required, no default
os.environ.setdefault("OUTBOX_PUBLISHER_ENABLED", "false")     # xAgent: no aiokafka
os.environ.setdefault("DB_POOL_OPEN_AT_STARTUP", "false")      # xAgent: never open a real pool
os.environ.setdefault("SWEEPER_ENABLED", "false")              # xAgent: no background sweeper
