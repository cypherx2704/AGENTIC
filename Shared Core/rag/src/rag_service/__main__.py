"""Run entry: ``python -m rag_service`` (cross-platform).

Sets the Windows SelectorEventLoop policy BEFORE the event loop is created — psycopg3's
async mode cannot run on Windows' default ProactorEventLoop. This MUST happen before
``uvicorn.run`` creates the loop. No-op on Linux/macOS, so production is unaffected.

When ``RAG_RUN_WORKER=1`` is set, runs the Kafka ingestion worker loop instead of the API
server (the dedicated worker process / compose service).
"""

from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _run_worker() -> None:  # pragma: no cover — live worker process
    from .core.config import get_settings
    from .db import pool as db_pool
    from .services.contextual import Contextualizer
    from .services.embeddings import EmbeddingClient
    from .services.object_store import ObjectStore
    from .services.service_token import ServiceTokenProvider
    from .worker.ingestion_worker import WorkerDeps, run_worker

    settings = get_settings()
    pool = db_pool.create_pool(settings.database_url)
    token_provider = ServiceTokenProvider(settings)
    deps = WorkerDeps(
        pool=pool,
        embedder=EmbeddingClient(settings, token_provider=token_provider),
        object_store=ObjectStore(settings),
        settings=settings,
        contextualizer=Contextualizer(settings, token_provider=token_provider),
    )

    async def _main() -> None:
        await pool.open(wait=False)
        await run_worker(deps)

    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_main())


def main() -> None:
    if os.getenv("RAG_RUN_WORKER") == "1":
        _run_worker()
        return

    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    if sys.platform == "win32":
        config = uvicorn.Config(
            "rag_service.main:app", host=host, port=port, log_config=None, loop="none"
        )
        server = uvicorn.Server(config)
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run("rag_service.main:app", host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
