# syntax=docker/dockerfile:1

# =============================================================================
# IMAGE TARGETS (WP07 — documented; the ml image is built on demand, not in CI default)
#
#   runtime       (DEFAULT, "slim") — this file as-is. No `ml` extra; the stub classifier
#                 runs (CLASSIFIER_MODE=stub). Light image, no torch/model download.
#                   docker build --target runtime -t guardrails:slim .
#
#   runtime-ml    — PROD detoxify image. Add `--extra ml` to the `uv sync` steps (pulls
#                 detoxify -> torch) and bake the PINNED checkpoint into the image so the
#                 EAGER load at lifespan is offline/airgap-safe. Run with
#                 CLASSIFIER_MODE=detoxify (DETOXIFY_CHECKPOINT pins the variant). Sketch:
#                   FROM builder AS builder-ml
#                   RUN uv sync --frozen --no-dev --extra ml
#                   RUN python -c "from detoxify import Detoxify; Detoxify('original')"  # prefetch
#                   FROM runtime AS runtime-ml
#                   COPY --from=builder-ml /app/.venv /app/.venv
#                   COPY --from=builder-ml /home/app/.cache /home/app/.cache  # model weights
#                   ENV CLASSIFIER_MODE=detoxify
#                 Build: docker build --target runtime-ml -t guardrails:ml .
#
# The stub image stays the default so `uv sync` / tests / local dev never pull torch; the
# service GRACEFULLY falls back to the stub if CLASSIFIER_MODE=detoxify but the dep/model
# is absent (services/classifier.py:build_classifier).
# =============================================================================

# ── Builder ───────────────────────────────────────────────────────────────────
# Resolve the frozen, production-only dependency set into /app/.venv. The 'ml'
# optional extra (detoxify -> torch) is intentionally NOT installed: the default
# CLASSIFIER_MODE=stub uses an in-process heuristic, keeping the image light.
FROM python:3.12-slim-bookworm AS builder

# uv: fast, reproducible installs from uv.lock.
RUN pip install --no-cache-dir uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer-cache the dependency resolution: only re-runs when the lock/manifest change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now bring in the source and install the project itself into the same venv.
COPY src ./src
RUN uv sync --frozen --no-dev

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# Non-root runtime user (uid 10001).
RUN useradd --system --uid 10001 --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

# Copy the resolved virtualenv and the application source from the builder.
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/src /app/src

# .venv/bin first on PATH so `python` is the venv interpreter; src on PYTHONPATH so
# `python -m guardrails_service` resolves without an editable install step.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# Liveness via stdlib only (no curl in the image). /livez is process-only — never
# touches DB/Kafka/classifier — so it stays green even before Neon wakes from suspend.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)"

USER 10001

CMD ["python", "-m", "guardrails_service"]
