# syntax=docker/dockerfile:1.6
#
# Polymarket Bot — multi-stage build (S4.11).
#
# Stage 1 (builder): installs build toolchain + Python deps into a
# virtualenv. The heavy ML deps (jax, scipy, lightgbm, numpyro) bring
# native wheels; we let pip pull pre-built wheels for linux/amd64 so
# this stays under ~3 min on the Oracle Free VM.
#
# Stage 2 (runtime): python:3.11-slim, copies the venv, drops to a
# non-root user, and ships the source. A single image is reused by
# observer / engine / registry — the `command` in docker-compose
# selects which entry point runs. Backtest deps (pandas + pyarrow) are
# installed too so `scripts/batch_runner.py` can run inside engine.

ARG PYTHON_VERSION=3.11

# --------------------------------------------------------------------------- #
# Stage 1 — builder                                                            #
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for native wheels (lightgbm, scipy fallback, asyncpg...).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libffi-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy pyproject first so the layer cache stays warm when source changes.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Create the venv inside the builder image, install the package + the
# backtest extra (pandas/pyarrow → required by the nightly batch).
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[backtest]"

# --------------------------------------------------------------------------- #
# Stage 2 — runtime                                                            #
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    LOG_LEVEL=INFO

# Minimal runtime libs.
#   libgomp1            — required by lightgbm + numpyro.
#   tini                — PID 1, reaps zombies and forwards signals.
#   postgresql-client   — pulls pg_dump/pg_restore for the S4.12
#                         backups service. Debian bookworm ships v15
#                         which matches our postgres:15 server.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        ca-certificates \
        tini \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. uid/gid 1000 keeps bind-mounted volumes happy on the
# Oracle Cloud VM (default user is uid 1000 there too).
RUN groupadd --system --gid 1000 polymarket \
    && useradd --system --uid 1000 --gid polymarket --create-home polymarket

# Copy the venv from the builder.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Source + scripts. We don't ship tests/ or .venv/.
# docs/migrations IS shipped: scripts/setup_db.py reads SQL files from
# /app/docs/migrations/ at boot to apply pending schema changes (S4 fix).
COPY --chown=polymarket:polymarket src/ ./src/
COPY --chown=polymarket:polymarket scripts/ ./scripts/
COPY --chown=polymarket:polymarket templates/ ./templates/
COPY --chown=polymarket:polymarket static/ ./static/
COPY --chown=polymarket:polymarket docs/migrations/ ./docs/migrations/
COPY --chown=polymarket:polymarket pyproject.toml README.md ./

USER polymarket

# tini reaps zombies and forwards signals correctly when the entry
# point is a python script (no shell wrapping).
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command: print module list. Each compose service overrides
# this with its own `command:` (observer / engine / registry / api).
CMD ["python", "-c", "print('Override CMD in docker-compose: observer | engine | registry | api')"]

# Image-level healthcheck — covers Redis + DB connectivity. compose
# overrides per-service if a tighter check is needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python /app/scripts/docker_healthcheck.py || exit 1
