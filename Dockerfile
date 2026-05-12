# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder: install Python deps into a private virtualenv.
# Heavy/transitive build tooling stays in this stage and is dropped from the
# final image so it never reaches production.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is needed by some wheels; gcc is dropped after install
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime: slim image, non-root user, only what we need to run.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    LOG_FORMAT=json

# curl is used by the docker-compose healthcheck.
# tini is a tiny init that reaps zombie processes — recommended for any container
# that runs Python (or any non-init PID 1).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 finops \
 && useradd  --system --uid 1001 --gid finops --home-dir /app --no-create-home --shell /usr/sbin/nologin finops

# Copy the prepared virtualenv from the builder stage
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Application code — copied as root so we can chown atomically below.
# Order matters: rarely-changing files first to maximise layer caching.
COPY --chown=finops:finops backend/ backend/
COPY --chown=finops:finops scripts/ scripts/
COPY --chown=finops:finops frontend/ frontend/
COPY --chown=finops:finops run_server.py .
# report_data.json is optional — generated at runtime if missing
COPY --chown=finops:finops report_data.json* ./

# Writable data dir for the SQLite findings DB. Mounted as a Docker volume in
# docker-compose.yml so it survives container restarts and image rebuilds.
RUN mkdir -p /app/data && chown finops:finops /app/data

USER finops

EXPOSE 8000

# tini → run_server.py: tini handles signals + zombie reaping so SIGTERM
# (docker stop) shuts the app down cleanly instead of being SIGKILLed after 10s.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run_server.py"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1
