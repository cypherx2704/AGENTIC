"""Process entrypoint.

Runs TWO uvicorn servers in one process:
  * :8080 (http)    — the full app: /echo, /livez, /readyz   (Kong routes here)
  * :9090 (metrics) — a tiny app exposing ONLY /metrics

The split matches the cypherx-service base chart port convention (ports.http
8080, ports.metrics 9090) and the Istio metrics-permissive PeerAuthentication
(Component 7): observability scrapes :9090 over plain HTTP while app traffic on
:8080 stays under STRICT mTLS.
"""

from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI

from .app import app as main_app
from .app import metrics as metrics_handler
from .config import settings

# Dedicated metrics-only app so /metrics is reachable on :9090.
metrics_app = FastAPI(title="echo-service-metrics", docs_url=None, redoc_url=None)
metrics_app.add_api_route("/metrics", metrics_handler, methods=["GET"])


async def _serve() -> None:
    http = uvicorn.Server(
        uvicorn.Config(
            main_app,
            host="0.0.0.0",  # noqa: S104 — in-cluster, fronted by Kong/Istio
            port=settings.http_port,
            log_config=None,  # we emit Contract 6 JSON ourselves
            access_log=False,
        )
    )
    metrics = uvicorn.Server(
        uvicorn.Config(
            metrics_app,
            host="0.0.0.0",  # noqa: S104
            port=settings.metrics_port,
            log_config=None,
            access_log=False,
        )
    )
    await asyncio.gather(http.serve(), metrics.serve())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
