# syntax=docker/dockerfile:1
# CypherX RAG service — multi-stage image (uv + pyproject + uv.lock).
# Module entrypoint: python -m rag_service. Listens on PORT=8080 inside the container.
# Set RAG_RUN_WORKER=1 to run the Kafka ingestion worker loop instead of the API server.

# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

# uv: fast, reproducible installs into a project-local virtualenv.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Resolve + install deps first (cached layer). psycopg[binary] bundles libpq —
# no system libpq needed. --no-dev keeps the dev group (ruff/pytest) out.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now bring in the source and install the project itself into the venv.
COPY src ./src
RUN uv sync --frozen --no-dev

# ── Runtime ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# Non-root user (uid 10001) per spec.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# Bring the built virtualenv + source from the builder stage.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0

EXPOSE 8080

# Process-only liveness probe via stdlib (no curl in the image).
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)"

USER 10001

# __main__ selects the Windows SelectorEventLoop only on win32 (no-op here on Linux).
CMD ["python", "-m", "rag_service"]
