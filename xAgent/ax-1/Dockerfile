# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# CypherX xAgent — agent-runtime (module: agent_runtime)
# Multi-stage build using uv + pyproject + uv.lock. Listens on PORT 8080.
# psycopg[binary] bundles libpq — no system libpq packages required.
# ─────────────────────────────────────────────────────────────────────────────

# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN pip install uv

WORKDIR /app

# Resolve + install dependencies into /app/.venv from the frozen lock (no dev group).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the project source and install the project itself into the venv.
COPY src ./src
RUN uv sync --frozen --no-dev

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# Non-root user (uid 10001).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# Bring the prebuilt virtualenv and the application source from the builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH=/app/.venv/bin:$PATH \
    PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

USER 10001

# Liveness probe via stdlib (no curl in the image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)"

CMD ["python", "-m", "agent_runtime"]
