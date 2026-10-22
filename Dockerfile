# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS runtime

# Werkzeug spools multipart bodies before the route runs; keep that spool on the
# same writable volume as B2ROUTER_TEMP_DIR instead of the image layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    B2ROUTER_HOST=0.0.0.0 \
    B2ROUTER_PORT=8000 \
    B2ROUTER_ACCOUNTS=/config/accounts.yaml \
    B2ROUTER_TEMP_DIR=/data/tmp \
    B2ROUTER_LOCK_FILE=/data/instance.lock \
    B2ROUTER_LOG_FORMAT=json \
    TMPDIR=/data/tmp

WORKDIR /app

# Install dependencies first so source edits do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Use an unprivileged account and writable state directories only.
RUN groupadd --gid 10001 b2router \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin b2router \
    && mkdir -p /data/tmp /config \
    && chown -R b2router:b2router /data \
    && chmod 700 /data/tmp

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# Flask's development server is intentional for 0.0.1: the cache, jobs, and
# flock are single-process state. Use exactly one replica.
CMD ["python", "-m", "b2router"]
