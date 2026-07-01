"""``python -m mcp_eng_memory`` entrypoint."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import uvicorn

    uvicorn.run(
        "mcp_eng_memory.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
