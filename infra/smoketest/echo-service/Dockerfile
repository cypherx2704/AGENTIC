# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# echo-service — CypherX Phase 1 infra smoke-test (Component 21)
# Multi-stage, non-root, slim. Builds a venv in a builder stage and copies the
# (already-installed) site-packages into a minimal runtime image.
# ---------------------------------------------------------------------------

# ---- Stage 1: builder ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Build deps only needed to compile wheels (asyncpg/aiokafka may build C ext).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated venv we can copy wholesale into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies first (better layer caching), then the package.
COPY pyproject.toml README.md ./
COPY echo_service ./echo_service
RUN pip install --upgrade pip \
    && pip install .

# ---- Stage 2: runtime ----
FROM python:3.12-slim AS runtime

# OCI labels for traceability.
LABEL org.opencontainers.image.title="echo-service" \
      org.opencontainers.image.description="CypherX Phase 1 infra smoke-test echo-service (Component 21)" \
      org.opencontainers.image.vendor="CypherX Platform Team" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root user/group with the same uid/gid the base chart's podSecurityContext
# pins (runAsUser/runAsGroup/fsGroup = 10001).
RUN groupadd --gid 10001 echo \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin echo

# Copy the prebuilt venv and the package source from the builder.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/echo_service /app/echo_service

WORKDIR /app
USER 10001:10001

# http (Kong-routed) + metrics (Prometheus scrape) ports.
EXPOSE 8080 9090

# Liveness path documented for non-K8s runtimes; K8s uses the chart probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/livez',timeout=2).status==200 else 1)"

ENTRYPOINT ["python", "-m", "echo_service"]
