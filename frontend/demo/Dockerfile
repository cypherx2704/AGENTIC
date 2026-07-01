################################################################################
# CypherX demo BFF — stdlib-only Python single-page agent runner.
# No pyproject / uv.lock: just server.py + index.html on the system Python.
# We install postgresql-client because server.py's _reset_bootstrap_sentinel
# shells out to `psql "$DEMO_DB_URL" -c "DELETE FROM auth.bootstrap_state;"`
# (Neon-compatible; there is NO postgres container).
################################################################################
FROM python:3.12-slim-bookworm

# psql for the Neon bootstrap-sentinel reset (best-effort, run at provisioning).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Non-root runtime user (uid 10001), matching the other CypherX images.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin appuser

# App is two stdlib files; copy them with the non-root user as owner.
COPY --chown=10001:10001 server.py index.html ./

ENV PORT=8090 \
    BIND=0.0.0.0 \
    AUTH_URL=http://auth-service:8080 \
    XAGENT_URL=http://xagent:8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8090

USER 10001

# Stdlib healthcheck — the BFF always answers /api/health (no auth required).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8090/api/health',timeout=3).status==200 else 1)"

CMD ["python", "server.py"]
