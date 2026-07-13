# syntax=docker/dockerfile:1
# CypherX tool-flow-bridge — multi-stage image (uv + pyproject + uv.lock).
# Module entrypoint: python -m tool_flow_bridge. Listens on PORT=8080 inside the container.

# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

# Resolve + install deps first (cached layer). --no-dev keeps the dev group out.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project

# Now bring in the source and install the project itself into the venv.
COPY src ./src
RUN uv sync --no-dev

# ── Runtime ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# Non-root user (uid 10001) per spec.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0

EXPOSE 8080

# Process-only liveness probe via stdlib (no curl in the image).
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)"

USER 10001

CMD ["python", "-m", "tool_flow_bridge"]
