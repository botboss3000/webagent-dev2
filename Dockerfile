# =============================================================================
# STAGE 1 — Build dependencies (Python wheels)
# =============================================================================
FROM python:3.14-slim AS builder

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the dependency manifest first — leverages Docker layer caching
# so rebuilds are lightning-fast when requirements haven't changed.
COPY requirements.txt .

RUN pip install --user --upgrade pip setuptools wheel && \
    pip install --user -r requirements.txt

# =============================================================================
# STAGE 2 — Runtime image (tiny, hardened)
# =============================================================================
FROM python:3.14-slim AS runtime

# ── Build metadata ────────────────────────────────────────────────────────────
LABEL description="WebAgent — FastAPI agent harness"

# ── Hardened system packages ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    tini \
    && rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory /app && \
    git config --system --add safe.directory /workspace/webagent

# ── Environment ───────────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8080 \
    HOME=/home/appuser \
    PLAYWRIGHT_BROWSERS_PATH=/home/appuser/.cache/ms-playwright

# ── Create non-root user ──────────────────────────────────────────────────────
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --gid 1001 --no-create-home appuser

# ── Copy built Python packages from builder ───────────────────────────────────
COPY --from=builder /root/.local /home/appuser/.local
RUN PYTHONPATH=/home/appuser/.local/lib/python3.14/site-packages \
    python -m playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* && \
    chown -R appuser:appgroup /home/appuser/.local /home/appuser/.cache

# Make sure the local binaries are on PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# ── Application code ──────────────────────────────────────────────────────────
WORKDIR /app
COPY --chown=appuser:appgroup . .

# ── Health check ──────────────────────────────────────────────────────────────
# Readiness includes a live database routing query; liveness alone is insufficient.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail http://localhost:${APP_PORT}/health/ready || exit 1

# ── Run ───────────────────────────────────────────────────────────────────────
USER appuser:appgroup
EXPOSE ${APP_PORT}

# tini reaps zombie processes and forwards signals cleanly
ENTRYPOINT ["/usr/bin/tini", "--"]
# WebAgent is an ASGI app (app.main:app) served by uvicorn on :8080 — the same
# server, port and WebSocket stack (wsproto) that run.py uses. Multi-worker is
# supported and correct: the singleton background loops run in ONE worker via the
# leader lock (app/coordination/leader.py), and cross-worker chat turns stream via
# the client DB-tail reconcile (see README / docs/claude/deployment.md). Single
# worker gives the smoothest WS streaming — set --workers 1 for that.
# --timeout-graceful-shutdown bounds worker shutdown. Uvicorn's default (unset)
# waits forever, so a worker holding a long-lived SSE/WebSocket connection never
# drains; Uvicorn's restart_all() then parks the supervisor in Process.join()
# (waitpid), its only signal-draining thread stops running, and EVERY later
# restart silently no-ops. See app/relauncher.py::_mp_reload.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4", "--ws", "wsproto", "--timeout-graceful-shutdown", "10"]
