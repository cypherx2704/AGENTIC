"""Process entrypoint: ``python -m cypherx_a1``.

Runs the FastAPI app by default. With ``CYPHERXA1_RUN_WORKER=1`` it runs the
ingestion/extraction Kafka worker loop instead (a separate process that drains the
``cypherx.cypherxa1.*`` work topics) — mirroring the rag-service ``RAG_RUN_WORKER`` split.

psycopg3 async cannot run on Windows' default ProactorEventLoop, so the SelectorEventLoop
policy is selected before the loop is created (a no-op on Linux/macOS).
"""

from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _run_api() -> None:
    import uvicorn

    uvicorn.run(
        "cypherx_a1.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_config=None,  # structlog owns logging (Contract 6)
    )


def _run_worker() -> None:
    from .worker.runner import run_worker

    asyncio.run(run_worker())


def main() -> None:
    if os.environ.get("CYPHERXA1_RUN_WORKER", "").strip() in ("1", "true", "yes"):
        _run_worker()
    else:
        _run_api()


if __name__ == "__main__":
    main()
