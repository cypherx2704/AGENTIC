"""Run entry: ``python -m memory_service`` (cross-platform).

Sets the Windows SelectorEventLoop policy BEFORE the event loop is created — psycopg3's
async mode cannot run on Windows' default ProactorEventLoop. This MUST happen before
``uvicorn.run`` creates the loop (setting it inside the app module is too late under the
uvicorn CLI). No-op on Linux/macOS, so production is unaffected.
"""

from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    if sys.platform == "win32":
        config = uvicorn.Config(
            "memory_service.main:app", host=host, port=port, log_config=None, loop="none"
        )
        server = uvicorn.Server(config)
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run("memory_service.main:app", host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
