# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# CypherX mcp-eng-memory — stateless MCP server (module: mcp_eng_memory)
# Multi-stage uv build. Listens on PORT 8080 in-container (host 8094 in compose).
# No DB / no Kafka — a thin facade over the cypherx-a1 product API.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install uv
WORKDIR /app
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-dev --no-install-project
COPY src ./src
COPY manifest.json ./manifest.json
RUN uv sync --no-dev

FROM python:3.12-slim-bookworm AS runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/manifest.json /app/manifest.json
ENV PATH=/app/.venv/bin:$PATH PORT=8080 HOST=0.0.0.0 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MANIFEST_PATH=/app/manifest.json
EXPOSE 8080
USER 10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)"
CMD ["python", "-m", "mcp_eng_memory"]
