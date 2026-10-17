# Coursework Portal — production image
#
# Deliberately thin: Python runtime, pinned dependencies, the app. Static files
# and templates are part of the package, so no build step is needed.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1 is required by numpy (shapely hashing / vectorised similarity).
# curl is used by the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# A non-root service account. The data volume is owned by it.
RUN groupadd --system portal && useradd --system --gid portal --home /srv/app portal \
    && mkdir -p /srv/app/data && chown -R portal:portal /srv/app/data

USER portal

ENV PORTAL_ENV=production \
    PORTAL_HOST=0.0.0.0 \
    PORTAL_PORT=8000 \
    PORTAL_DATA_DIR=/srv/app/data \
    DRIVE_PROVIDER=mock

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# A single process by default: the analysis pool is CPU-bound and sized to the
# container's cores. Scale with more replicas (see docker-compose.yml) rather
# than with --workers, so the background analysis queues stay per-replica and
# the work is not duplicated.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
