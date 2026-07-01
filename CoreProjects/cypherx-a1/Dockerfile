# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# CypherX cypherx-a1 — Autonomous Engineering Memory (module: cypherx_a1)
# Multi-stage uv build. Listens on PORT 8080 in-container (host 8093 in compose).
# psycopg[binary] bundles libpq — no system libpq packages required.
# Mirrors the xAgent ax-1 image contract: non-root uid 10001, stdlib HEALTHCHECK.
# ─────────────────────────────────────────────────────────────────────────────

# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN pip install uv

WORKDIR /app

# Resolve + install dependencies. uv.lock is committed; fall back to a non-frozen
# resolve if the lock is absent so the image still builds in a fresh checkout.
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY src ./src
RUN uv sync --no-dev

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH=/app/.venv/bin:$PATH \
    PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

USER 10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)"

CMD ["python", "-m", "cypherx_a1"]
