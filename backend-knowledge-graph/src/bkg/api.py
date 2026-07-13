"""Backend Intelligence API — a thin HTTP/REST transport over :class:`GraphService`.

Mirrors the CLI and the MCP server exactly: a presentation-independent surface with
**no LLM on the query path** (pure, deterministic index lookups). Future clients — a
web UI / Postman replacement, a VS Code panel, the request-execution runner — sit on
this seam instead of on the graph internals.

Freshness + single-owner concurrency (same model as ``bkg-mcp``): the server owns a
:class:`Daemon` and calls ``resync()`` before every request, so a query always sees
the current source (updated incrementally). The engine writes-on-read (memoization),
so there is no read-only replica — a module-level lock serializes the single SQLite
connection, and a startup owner-lock refuses to run alongside another owner
(``bkg watch`` / ``bkg-mcp`` / another ``bkg serve``) on the same project.

FastAPI + uvicorn are an OPTIONAL dependency (``bkg[api]``); the core never imports
them. ``build_app`` imports fastapi lazily so importing this module stays cheap.

Composite ``(repo_id, id)`` addressing at the boundary (future-proofing for
cross-repository, roadmap Phase 3): every served entity carries a ``repo`` field and
routes are also mounted under ``/repos/{repo_id}/graph/...`` — the graph-internal ids
stay ``{file}:...`` (frozen identity), so no re-keying migration is needed later.

NOTE: this module imports fastapi at top level, so it must only be imported when the
optional ``bkg[api]`` extra is present. The core never imports it — the CLI
lazy-imports ``serve`` inside ``bkg serve`` — so the contract holds.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .service import GraphService, _match_method, _match_tag


def _filter_endpoints(
    svc: GraphService, q: str | None, method: str | None, tag: str | None
) -> list[dict[str, Any]]:
    """Compose the registry search + method/tag filters (all pure index lookups)."""
    endpoints = svc.search_endpoints(q) if q else svc.list_endpoints()
    if method:
        endpoints = [ep for ep in endpoints if _match_method(ep, method.strip().upper())]
    if tag:
        endpoints = [ep for ep in endpoints if _match_tag(ep, tag.strip().lower())]
    return endpoints


def build_app(
    service: GraphService,
    refresh: Callable[[], None] | None = None,
    repo_id: str = "default",
    cors_origins: list[str] | None = None,
) -> Any:
    """Build the FastAPI app over ``service``. ``refresh`` (e.g. ``daemon.resync``) is
    called before every request under the serialization lock, so responses are fresh."""
    configured_repo = repo_id
    lock = threading.Lock()
    app = FastAPI(title="bkg — Backend Intelligence API", version="0.0.0")

    # Localhost-only CORS by default: an unauthenticated local service with a '*'
    # origin is a CSRF surface. A browser UI on localhost is allowed; widen explicitly.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_origins=cors_origins or [],
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    def fresh_service() -> Iterator[GraphService]:
        # Hold the lock across the whole request: the engine + its SQLite connection
        # are single-threaded, and resync writes-on-read. Sync handlers run in the
        # threadpool, so without this two requests could collide on one connection.
        with lock:
            if refresh is not None:
                refresh()
            yield service

    def check_repo(repo_id: str) -> None:
        if repo_id != configured_repo:
            raise HTTPException(status_code=404, detail=f"unknown repo {repo_id!r}")

    def _decorate(entity: dict[str, Any]) -> dict[str, Any]:
        return {**entity, "repo": configured_repo}

    def _envelope(request: Request, data: Any) -> Response:
        digest = service.snapshot_digest()
        # ETag must key on the graph digest AND the request identity (path + query):
        # the digest alone is the whole-graph state, so two different routes/queries
        # would share it and a stale If-None-Match from one would 304 the other.
        tag_src = f"{configured_repo}\x00{digest}\x00{request.url.path}\x00{request.url.query}"
        etag = '"' + hashlib.blake2b(tag_src.encode(), digest_size=16).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        payload: dict[str, Any] = {"repo": configured_repo, "digest": digest, "data": data}
        if isinstance(data, list):
            payload["count"] = len(data)
        return JSONResponse(payload, headers={"ETag": etag})

    graph = APIRouter()

    @graph.get("/endpoints")
    def list_endpoints(
        request: Request,
        svc: GraphService = Depends(fresh_service),
        q: str | None = None,
        method: str | None = None,
        tag: str | None = None,
    ) -> Response:
        eps = [_decorate(ep) for ep in _filter_endpoints(svc, q, method, tag)]
        return _envelope(request, eps)

    @graph.get("/search")
    def search(
        request: Request,
        svc: GraphService = Depends(fresh_service),
        q: str | None = None,
        method: str | None = None,
        tag: str | None = None,
    ) -> Response:
        eps = [_decorate(ep) for ep in _filter_endpoints(svc, q, method, tag)]
        return _envelope(request, eps)

    @graph.get("/endpoints/by-id")
    def get_endpoint_by_id(
        request: Request, id: str, svc: GraphService = Depends(fresh_service)
    ) -> Response:
        ep = svc.get_endpoint_by_id(id)
        if ep is None:
            raise HTTPException(status_code=404, detail=f"no endpoint {id!r}")
        return _envelope(request, _decorate(ep))

    @graph.get("/endpoints/{method}/{resolved_path:path}")
    def get_endpoint(
        request: Request, method: str, resolved_path: str, svc: GraphService = Depends(fresh_service)
    ) -> Response:
        # resolved paths are stored with a leading '/'; tolerate single- or double-slash URLs
        path = resolved_path if resolved_path.startswith("/") else "/" + resolved_path
        ep = svc.get_endpoint(method, path)
        if ep is None:
            raise HTTPException(status_code=404, detail=f"no endpoint {method.upper()} {path}")
        return _envelope(request, _decorate(ep))

    @graph.get("/schemas")
    def list_schemas(request: Request, svc: GraphService = Depends(fresh_service)) -> Response:
        return _envelope(request, [_decorate(s) for s in svc.list_schemas()])

    @graph.get("/config")
    def list_config(request: Request, svc: GraphService = Depends(fresh_service)) -> Response:
        return _envelope(request, [_decorate(c) for c in svc.list_config()])

    @graph.get("/blast-radius/{schema_id:path}")
    def blast_radius(
        request: Request, schema_id: str, svc: GraphService = Depends(fresh_service)
    ) -> Response:
        # empty is a valid answer (DTO unreferenced), not a 404
        return _envelope(request, svc.blast_radius(schema_id))

    @graph.get("/trust")
    def trust(request: Request, svc: GraphService = Depends(fresh_service)) -> Response:
        return _envelope(request, svc.trust_summary())

    # Default-repo alias + composite repo-scoped mount (Phase-3 future-proofing).
    # POST /runner/execute and /runner/test are RESERVED for the request-execution
    # engine (roadmap Phase 6) — unregistered here, so they 404 (read-only surface).
    app.include_router(graph, prefix="/graph")
    app.include_router(graph, prefix="/repos/{repo_id}/graph", dependencies=[Depends(check_repo)])

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "repo": configured_repo, "files": sorted(service.files())}

    return app


class _OwnerLock:
    """A best-effort single-owner lock at ``<root>/.bkg/owner.lock`` (atomic O_EXCL +
    pid). Makes the deferred single-owner guard concrete now that ``bkg serve`` makes
    concurrent owners on one project likely (a dev running ``watch`` + ``serve``)."""

    def __init__(self, root: str) -> None:
        bkg = os.path.join(root, ".bkg")
        os.makedirs(bkg, exist_ok=True)
        self._path = os.path.join(bkg, "owner.lock")
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError(
                f"another bkg owner is running on this project (lock: {self._path}). "
                "Stop `bkg watch` / `bkg-mcp` / another `bkg serve` first, "
                "or delete the lock file if it is stale."
            ) from None
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.remove(self._path)
        except OSError:
            pass


def serve(
    directory: str,
    host: str | None = None,
    port: int | None = None,
    repo_id: str | None = None,
    cors_origins: list[str] | None = None,
) -> None:  # pragma: no cover - blocking server
    """Own a :class:`Daemon` on ``directory`` and serve the HTTP API (blocking)."""
    import uvicorn

    host = host or os.environ.get("BKG_API_HOST", "127.0.0.1")
    port = port or int(os.environ.get("BKG_API_PORT", "8765"))
    repo = repo_id or os.path.basename(os.path.abspath(directory)) or "default"

    from .daemon import Daemon

    owner = _OwnerLock(directory)
    owner.acquire()
    try:
        daemon = Daemon(directory)  # warm from <root>/.bkg/graph.db; initial resync here
        app = build_app(daemon.service, refresh=daemon.resync, repo_id=repo, cors_origins=cors_origins)
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        owner.release()


def main() -> None:  # pragma: no cover - blocking server
    serve(os.environ.get("BKG_PROJECT", "."))
