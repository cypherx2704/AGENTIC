"""Run entry: ``python -m agent_runtime`` (cross-platform).

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
    port = int(os.getenv("PORT", "8080"))

    if sys.platform == "win32":
        # Run the server explicitly on a SelectorEventLoop and tell uvicorn NOT to manage
        # the loop (loop="none"); otherwise uvicorn re-selects the Proactor loop and
        # psycopg breaks.
        config = uvicorn.Config("agent_runtime.main:app", host=host, port=port, log_config=None, loop="none")
        server = uvicorn.Server(config)
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run("agent_runtime.main:app", host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
